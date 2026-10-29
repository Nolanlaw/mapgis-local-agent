// Bind only this owned thread. Never switch the user's input desktop.
#pragma once
#include <windows.h>
#include <stdexcept>
inline void bind_private_desktop(){
 wchar_t name[128]{};
 DWORD length=GetEnvironmentVariableW(L"MAPGIS_AGENT_DESKTOP",name,128);
 if(!length)return; // Direct CLI use can inherit its caller's desktop.
 if(length>=128||wcsncmp(name,L"MapGISAgent_",12)!=0)throw std::runtime_error("private desktop name");
 HDESK desktop=OpenDesktopW(name,0,FALSE,0xC7);
 if(!desktop)throw std::runtime_error("private desktop open");
 if(!SetThreadDesktop(desktop)){CloseDesktop(desktop);throw std::runtime_error("private desktop bind");}
 // This handle must remain open while the thread is attached; the process
 // releases it on exit, including abrupt termination.
}
