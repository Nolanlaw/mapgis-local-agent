// Exact-build, read-only projection parameter views from the 4.x and 5.x readers.
#pragma once
#include <array>

namespace native_l2 {
struct Profile {
    const char* format;
    const char* digest;
    const char* constructor;
    const char* destructor;
    const char* count;
    const char* get;
    size_t block_size;
    short declared_slots;
};

constexpr Profile legacy4{
    "4x", "cc6794165ed0316cc6820b07e21d03685bde680bacc67b3bdd612c7623fce3a9",
    "??0C4x_MAP_PARA@@QAE@PAX@Z", "??1C4x_MAP_PARA@@UAE@XZ",
    "?GetItemNum@C4x_MAP_PARA@@QAEFXZ", "?GetItemValue@C4x_MAP_PARA@@QAENF@Z", 33, 29
};
constexpr Profile legacy5{
    "5x", "17c2c4808e48a38da937c8abd486450fa0d8536e8bff0c3794b6f209ae955779",
    "??0C50_MAP_PARA@@QAE@PAX@Z", "??1C50_MAP_PARA@@UAE@XZ",
    "?GetItemNum@C50_MAP_PARA@@QAEFXZ", "?GetItemValue@C50_MAP_PARA@@QAENF@Z", 56, 17
};

// Each constructor stores only the vtable and a borrowed packed-block pointer.
struct ParameterView {
    void* vtable;
    unsigned char* raw;
};
static_assert(sizeof(ParameterView) == 8 && sizeof(void*) == 4, "verified x86 parameter view");
using Construct = ParameterView* (__thiscall*)(ParameterView*, void*);
using Destroy = void (__thiscall*)(ParameterView*);
using SlotCount = short (__thiscall*)(ParameterView*);
using ReadSlot = double (__thiscall*)(ParameterView*, short);

static unsigned windows_created = 0;
static unsigned popup_events = 0;

inline LRESULT CALLBACK window_creation(int code, WPARAM window, LPARAM data) {
    if (code == HCBT_CREATEWND) {
        ++windows_created;
        const auto* create = reinterpret_cast<const CBT_CREATEWNDW*>(data)->lpcs;
        const auto name = create->lpszClass;
        if ((create->style & WS_VISIBLE) || name == MAKEINTRESOURCEW(32770) ||
            (name && !IS_INTRESOURCE(name) && !wcscmp(name, L"#32770")))
            ++popup_events;
    }
    if (code == HCBT_ACTIVATE) ++popup_events;
    return CallNextHookEx(nullptr, code, window, data);
}

inline LRESULT CALLBACK window_display(int code, WPARAM event, LPARAM data) {
    if (code >= 0) {
        const auto& message = *reinterpret_cast<const CWPSTRUCT*>(data);
        if ((message.message == WM_SHOWWINDOW && message.wParam) ||
            (message.message == WM_WINDOWPOSCHANGING && message.lParam &&
             (reinterpret_cast<const WINDOWPOS*>(message.lParam)->flags & SWP_SHOWWINDOW)))
            ++popup_events;
    }
    return CallNextHookEx(nullptr, code, event, data);
}

struct WindowTrace {
    HHOOK creation = SetWindowsHookExW(WH_CBT, window_creation, nullptr, GetCurrentThreadId());
    HHOOK display = SetWindowsHookExW(WH_CALLWNDPROC, window_display, nullptr, GetCurrentThreadId());
    ~WindowTrace() {
        if (display) UnhookWindowsHookEx(display);
        if (creation) UnhookWindowsHookEx(creation);
    }
};

struct Library {
    HMODULE handle;
    ~Library() { if (handle) FreeLibrary(handle); }
};

inline std::vector<std::pair<short, double>> read_items(const Profile& profile, const wchar_t* library_path,
                                                       std::array<unsigned char, 56>& raw) {
    if (digest(library_path) != profile.digest) throw std::runtime_error("L2 DLL build mismatch");
    constexpr DWORD search = LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32;
    Library library{LoadLibraryExW(library_path, nullptr, search)};
    if (!library.handle) throw std::runtime_error("L2 native load failed: " + std::to_string(GetLastError()));
    wchar_t loaded[32768]{};
    const auto length = GetModuleFileNameW(library.handle, loaded, _countof(loaded));
    if (!length || length == _countof(loaded) || _wcsicmp(loaded, library_path) ||
        digest(loaded) != profile.digest)
        throw std::runtime_error("L2 loaded module identity mismatch");
    auto construct = sym<Construct>(library.handle, profile.constructor);
    auto destroy = sym<Destroy>(library.handle, profile.destructor);
    auto count = sym<SlotCount>(library.handle, profile.count);
    auto read = sym<ReadSlot>(library.handle, profile.get);
    std::vector<std::pair<short, double>> items;
    items.reserve(17);
    ParameterView view{};
    if (construct(&view, raw.data()) != &view || view.raw != raw.data())
        throw std::runtime_error("L2 projection view layout");
    const short actual_slots = count(&view);
    if (actual_slots == profile.declared_slots) {
        // The 4.x getter's jump table only reads slots 0 and 2..8. Other
        // indices return a constant. The 5.x accessor reads every slot.
        const short limit = &profile == &legacy4 ? 9 : actual_slots;
        for (short index = 0; index < limit; ++index)
            if (&profile != &legacy4 || index != 1) items.emplace_back(index, read(&view, index));
    }
    destroy(&view);
    if (actual_slots != profile.declared_slots) throw std::runtime_error("L2 projection slot count");
    return items;
}

inline int run(wchar_t** argv) {
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    try {
        const auto desktop = desktop_name();
        DWORD dep = 0;
        BOOL permanent = FALSE;
        if (desktop.rfind("MapGISAgent_", 0) != 0 ||
            !GetProcessDEPPolicy(GetCurrentProcess(), &dep, &permanent) || dep != 0)
            throw std::runtime_error("L2 private x86 legacy host required");
        const auto* profile = !wcscmp(argv[2], L"4x") ? &legacy4 :
                              !wcscmp(argv[2], L"5x") ? &legacy5 : nullptr;
        if (!profile) throw std::runtime_error("L2 format is outside the allowlist");
        std::array<unsigned char, 56> block{};
        std::ifstream file(argv[4], std::ios::binary);
        if (!file.read(reinterpret_cast<char*>(block.data()), profile->block_size) ||
            file.peek() != std::char_traits<char>::eof())
            throw std::runtime_error("L2 packed parameter block size");
        const auto original = block;
        printf("{\"event\":\"host\",\"protocol\":1,\"pid\":%lu,\"bits\":32,\"dep_flags\":%lu,\"desktop\":\"%s\"}\n",
               GetCurrentProcessId(), dep, desktop.c_str());
        fflush(stdout);
        WindowTrace trace;
        if (!trace.creation || !trace.display) throw std::runtime_error("L2 window observation unavailable");
        auto items = read_items(*profile, argv[3], block);
        printf("{\"event\":\"l2_window_trace\",\"windows_created\":%u,\"popup_events\":%u}\n",
               windows_created, popup_events);
        fflush(stdout);
        if (windows_created || popup_events) throw std::runtime_error("L2 DLL attempted window creation or display");
        if (block != original) throw std::runtime_error("L2 changed the input buffer");
        for (const auto& item : items)
            if (!std::isfinite(item.second)) throw std::runtime_error("L2 non-finite projection item");
        printf("{\"event\":\"l2_projection_items\",\"format\":\"%s\",\"declared_slots\":%d,"
               "\"input_preserved\":true,\"items\":[", profile->format, profile->declared_slots);
        for (size_t i = 0; i < items.size(); ++i)
            printf("%s{\"index\":%d,\"value\":%.17g}", i ? "," : "", items[i].first, items[i].second);
        puts("]}");
        puts("{\"event\":\"complete\"}");
        return 0;
    } catch (const std::exception& error) {
        fprintf(stderr, "%s\n", error.what());
        return 1;
    }
}
} // namespace native_l2
