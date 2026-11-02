// Pinned original Tin3DView on an owned hidden software-OpenGL window.
#pragma once
namespace native_tin_scene {
using native_scene::check;
#pragma pack(push,1)
struct Camera {char magic[8];double origin[2];float scales[3],eye[3],target[3],up[3],fov;};
#pragma pack(pop)
static_assert(sizeof(Camera)==76,"owned TIN camera format");
using Create=void*(__cdecl*)();using Drop=short(__stdcall*)(void*);using Dirty=short(__stdcall*)(void*,int);
using Read=short(__stdcall*)(void*,int,void*);using Append=int(__stdcall*)(void*,void*);
struct Area {
 HMODULE module;void* value=nullptr;
 ~Area(){if(value){sym<Dirty>(module,MAKEINTRESOURCEA(10))(value,0);sym<Drop>(module,MAKEINTRESOURCEA(5))(value);}}
};
struct View {
 void* value=nullptr;HWND window=nullptr;
 ~View(){if(value){HWND empty=nullptr;memcpy(static_cast<unsigned char*>(value)+32,&empty,4);using Delete=void(__thiscall*)(void*,unsigned);auto table=*reinterpret_cast<void***>(value);reinterpret_cast<Delete>(table[1])(value,1);}if(window){DestroyWindow(window);UnregisterClassW(L"MapGISAgentOriginal3D",GetModuleHandleW(nullptr));}}
};
unsigned char* state(void* view){unsigned char* result=nullptr;memcpy(&result,static_cast<unsigned char*>(view)+2402,4);check(result!=nullptr,"owned native TIN state");return result;}
Camera camera(void* view,const double* origin){
 auto data=state(view);Camera pose{};memcpy(pose.magic,"MGTK0002",8);memcpy(pose.origin,origin,16);
 memcpy(pose.scales,data+1053759,12);memcpy(pose.eye,data+1053723,12);memcpy(pose.target,data+1053735,12);memcpy(pose.up,data+1053711,12);memcpy(&pose.fov,data+1053691,4);return pose;
}
void validate(const Camera& pose){
 check(!memcmp(pose.magic,"MGTK0002",8),"native TIN camera profile");for(double v:pose.origin)check(std::isfinite(v),"TIN origin");
 for(float v:pose.scales)check(std::isfinite(v)&&v>0,"TIN positive scales");
 for(float v:pose.eye)check(std::isfinite(v),"TIN camera eye");for(float v:pose.target)check(std::isfinite(v),"TIN camera target");for(float v:pose.up)check(std::isfinite(v),"TIN camera up");
 check(std::isfinite(pose.fov)&&pose.fov>=30&&pose.fov<=120,"TIN field of view");
 double dir[3]{},cross[3]{},d=0,u=0,c=0;for(int i=0;i<3;++i){dir[i]=double(pose.eye[i])-pose.target[i];d+=dir[i]*dir[i];u+=double(pose.up[i])*pose.up[i];}
 for(int i=0;i<3;++i){cross[i]=dir[(i+1)%3]*pose.up[(i+2)%3]-dir[(i+2)%3]*pose.up[(i+1)%3];c+=cross[i]*cross[i];}check(d>0&&u>0&&c>1e-16*d*u,"TIN camera direction");
}
void local_pose(HMODULE base,void* view,const Camera& pose){
 validate(pose);auto data=state(view);
 if(!memcmp(data+1053723,pose.eye,12)&&!memcmp(data+1053735,pose.target,12)&&!memcmp(data+1053711,pose.up,12))return;
 memcpy(data+1053723,pose.eye,12);memcpy(data+1053735,pose.target,12);memcpy(data+1053711,pose.up,12);
 using Look=long(__thiscall*)(void*,const float*,const float*,const float*);
 check(sym<Look>(base,"?SetLookAtVec@C3DView@@QAEJPAUG3D_VERTEX@@00@Z")(view,pose.eye,pose.target,pose.up)>0,"original TIN local camera");
}
void bind_texture(HMODULE module,void* view,const std::wstring& folder,const double* bounds){
 auto path=ansi((folder+L"/texture.msi").c_str());if(GetFileAttributesA(path.data())==INVALID_FILE_ATTRIBUTES)return;
 auto tex=GetModuleHandleW(L"TexDbs.dll");check(tex!=nullptr,"original texture module");
 native_scene::TextureOptions options{0,1,2,1,0,1024};native_scene::TextureInfo info{};void* buffer=nullptr;
 using Extract=short(__stdcall*)(const char*,const double*,void*,void*,void**);double domain[4]{0,0,bounds[2]-bounds[0],bounds[3]-bounds[1]};
 check(sym<Extract>(tex,"_ExtractImageFormMsi")(path.data(),domain,&options,&info,&buffer)>0,"original MSI texture extraction");
 check(info.kind==1&&info.channels==3&&info.width>=128&&info.height>=128&&info.width<=1024&&info.height<=1024&&info.bytes==unsigned(info.width)*info.height*3&&!info.gl_format&&buffer,"original extracted RGB texture");
 auto data=state(view);unsigned existing=0;memcpy(&existing,data+1053328,4);check(!existing&&!data[1053332],"empty owned TIN texture state");
 memcpy(data+1053298,&info,sizeof(info));unsigned format=0;using Format=int(__cdecl*)(unsigned char,unsigned*);
 reinterpret_cast<Format>(reinterpret_cast<unsigned char*>(module)+0xc8b0)(info.kind,&format);check(format==1,"original RGB format");
 unsigned extracted=1;memcpy(data+1053316,&format,4);memcpy(data+1053320,&extracted,4);memcpy(data+1053328,&buffer,4);data[1053332]=1;
 using Surface=short(__thiscall*)(void*,int);check(sym<Surface>(module,"?SetSurfaceDispMode@CTin3DView@@QAEFW4_SurfaceDispMode@@@Z")(view,0)>0,"original TIN textured surface");
 printf("{\"event\":\"tin_texture_bound\",\"width\":%u,\"height\":%u,\"bytes\":%u,\"kind\":%u}\n",info.width,info.height,info.bytes,info.kind);fflush(stdout);
}
#include "NativeTinLines.h"
#include "NativeTinFlight.h"
int run(wchar_t** argv){
 SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
 try{
   bool inspecting=!wcscmp(argv[2],L"inspect");check(inspecting||!wcscmp(argv[2],L"render"),"TIN scene command");std::wstring program=argv[3],folder=argv[5];SetDllDirectoryW(program.c_str());check(SetCurrentDirectoryW(folder.c_str())!=0,"TIN scene owned working directory");
  DWORD dep=0;BOOL permanent=FALSE;check(GetProcessDEPPolicy(GetCurrentProcess(),&dep,&permanent)&&!dep,"owned legacy TIN process");
  printf("{\"event\":\"host\",\"pid\":%lu,\"bits\":32,\"dep_flags\":%lu,\"desktop\":\"%s\"}\n",GetCurrentProcessId(),dep,desktop_name().c_str());fflush(stdout);
   if(inspecting){native_scene::inspect_flight(folder);puts("{\"event\":\"complete\"}");fflush(stdout);return 0;}
  const std::pair<const wchar_t*,const char*> originals[]={{L"bas60.dll",BAS_SHA},
   {L"C3DView.dll","2e5cb1425d23d9517317458cc2fd66e1572f07fe4ab8b54f102696f156f4eca4"},
   {L"COglDraw.dll","74ae1996c7852c079ecb18c7884de1c0835e5c45551de0cfe8eabd6d61b88403"},
   {L"Tin3DView.dll","05de544e830293a282b25adbcfeb8b0b4f67c2b5d49e6dedca6c2251a9351783"},
   {L"Tin3DBase.dll","4d40a8467a889f7cc279b00fe27310fbb40b11f43954188cb22965b839dd8a35"},
   {L"W60_Tin.dll","94e25abdb102fb50d791aa689b114fddd48dfecf9eb24a2a82329ed4ddce0404"},
   {L"TinBas60.dll","ac02d4eb00fcf2dfa9b8f64703f08b28af4bfa171bf60d9cb15d93d341df8270"},
   {L"basroot.dll","d1109d09d3708911a68a490fc86d64e8fbf30b3f53a7ffce8de2241ec5cc36ba"},
   {L"mapdsp60.dll","e1995ea477c7d87bd6ed75d7327376b9a5f9ed8d2564b0a42dbada9197b671cb"},
   {L"TexDbs.dll","b6b6fa0d701c9ff939343eaf87a3b7e7e188ec4626f89205c25248cc5c363c08"}};
  for(auto& original:originals)check(digest((program+L"/"+original.first).c_str())==original.second,"original TIN view fingerprint");
  int width=0,height=0,scale_flag=0,world_flag=0;float fov=0,scales[3]{};double eye[3]{},target[3]{};
  auto settings_name=ansi(argv[6]);std::ifstream settings(settings_name.data());settings.imbue(std::locale::classic());
  settings>>width>>height>>fov>>scale_flag>>scales[0]>>scales[1]>>scales[2]>>world_flag>>eye[0]>>eye[1]>>eye[2]>>target[0]>>target[1]>>target[2];std::string extra;
  check(settings&&!(settings>>extra)&&width>=32&&width<=2048&&height>=32&&height<=2048&&std::isfinite(fov)&&fov>=30&&fov<=120&&(scale_flag==0||scale_flag==1)&&(world_flag==0||world_flag==1),"TIN scene settings");
  for(float v:scales)check(std::isfinite(v)&&v>0,"TIN scale option");for(double v:eye)check(std::isfinite(v),"TIN world eye");for(double v:target)check(std::isfinite(v),"TIN world target");
  check(LoadLibraryW((program+L"/bas60.dll").c_str())!=nullptr,"original BAS load");
  auto module=LoadLibraryW((program+L"/Tin3DView.dll").c_str());check(module!=nullptr,"original TIN view load");
  auto tin=GetModuleHandleW(L"W60_Tin.dll"),base=GetModuleHandleW(L"C3DView.dll");check(tin&&base,"original TIN dependencies");terrain_temp_folder((folder+L"/scene.tiles").c_str());
  Area source{tin},local{tin};source.value=sym<Create>(tin,MAKEINTRESOURCEA(4))();check(source.value!=nullptr,"native TIN source area");
  using Load=short(__stdcall*)(void*,char*);auto name=ansi(argv[4]);require(sym<Load>(tin,MAKEINTRESOURCEA(15))(source.value,name.data()),"native TIN source load");
  using Counts=short(__stdcall*)(void*,int*,int*);int points=0,ps=0,triangles=0,ts=0;
  require(sym<Counts>(tin,MAKEINTRESOURCEA(53))(source.value,&points,&ps),"TIN point counts");require(sym<Counts>(tin,MAKEINTRESOURCEA(54))(source.value,&triangles,&ts),"TIN face counts");
  check(points==ps-1&&triangles==ts-1&&points>=3&&points<=100000&&triangles>=1&&triangles<=200000,"dense native TIN display profile");
  using Bounds=short(__stdcall*)(void*,double*);double bounds[4]{};require(sym<Bounds>(tin,MAKEINTRESOURCEA(56))(source.value,bounds),"native canonical TIN bounds");for(double v:bounds)check(std::isfinite(v),"TIN source bounds");
  local.value=sym<Create>(tin,MAKEINTRESOURCEA(4))();check(local.value!=nullptr,"owned local display area");
  for(int i=1;i<=points;++i){NativeTinPoint point{};require(sym<Read>(tin,MAKEINTRESOURCEA(21))(source.value,i,&point),"canonical TIN vertex");point.x-=bounds[0];point.y-=bounds[1];check(sym<Append>(tin,MAKEINTRESOURCEA(20))(local.value,&point)==i,"owned local vertex");}
  for(int i=1;i<=triangles;++i){NativeTinTriangle face{};require(sym<Read>(tin,MAKEINTRESOURCEA(32))(source.value,i,&face),"canonical TIN face");check(sym<Append>(tin,MAKEINTRESOURCEA(31))(local.value,&face)==i,"owned local face");}
  for(int i=1;i<=points;++i){NativeTinPoint expected{},actual{};require(sym<Read>(tin,MAKEINTRESOURCEA(21))(source.value,i,&expected),"canonical vertex recheck");expected.x-=bounds[0];expected.y-=bounds[1];require(sym<Read>(tin,MAKEINTRESOURCEA(21))(local.value,i,&actual),"local vertex recheck");check(!memcmp(&expected,&actual,sizeof(actual)),"complete TIN vertex record preserved");}
  for(int i=1;i<=triangles;++i){NativeTinTriangle expected{},actual{};require(sym<Read>(tin,MAKEINTRESOURCEA(32))(source.value,i,&expected),"canonical face recheck");require(sym<Read>(tin,MAKEINTRESOURCEA(32))(local.value,i,&actual),"local face recheck");check(!memcmp(&expected,&actual,sizeof(actual)),"complete TIN face topology preserved");}
  double normalized[4]{};require(sym<Bounds>(tin,MAKEINTRESOURCEA(56))(local.value,normalized),"local display bounds");check(normalized[0]==0&&normalized[1]==0&&normalized[2]==bounds[2]-bounds[0]&&normalized[3]==bounds[3]-bounds[1],"local display extent");
  View owned;using Factory=void*(__stdcall*)();owned.value=sym<Factory>(module,"?CreateObject@CTin3DView@@SGPAVCObject@@XZ")();check(owned.value!=nullptr,"original view factory");
  owned.window=native_scene::software_window(width,height);HWND before=nullptr;memcpy(&before,static_cast<unsigned char*>(owned.value)+32,4);check(before==nullptr,"owned new view window");memcpy(static_cast<unsigned char*>(owned.value)+32,&owned.window,4);
  using Initial=void(__thiscall*)(void*);sym<Initial>(module,"?InitialOpenGL@CTin3DView@@AAEXXZ")(owned.value);
  const char* renderer=reinterpret_cast<const char*>(glGetString(GL_RENDERER));check(renderer&&!strcmp(renderer,"GDI Generic")&&wglGetCurrentContext()&&WindowFromDC(wglGetCurrentDC())==owned.window,"owned original software GL context");
  sym<Initial>(module,"?InitialEnvirnment@CTin3DView@@AAEXXZ")(owned.value);using Model=short(__thiscall*)(void*,void*);check(sym<Model>(module,"?InitialTinScene@CTin3DView@@QAEFPAVOTin_Area@@@Z")(owned.value,local.value)>0,"original TIN scene preparation");
  using Size=void(__thiscall*)(void*,unsigned,int,int);sym<Size>(module,"?OnSize@CTin3DView@@IAEXIHH@Z")(owned.value,0,width,height);
  using Scales=short(__thiscall*)(void*,float*);using Fov=short(__thiscall*)(void*,float);
  auto input_name=ansi((folder+L"/camera-input.bin").c_str());bool restored=GetFileAttributesA(input_name.data())!=INVALID_FILE_ATTRIBUTES;
  Camera pose{};
  if(restored){
   check(!scale_flag&&!world_flag,"saved TIN camera options");std::ifstream in(input_name.data(),std::ios::binary);in.read(reinterpret_cast<char*>(&pose),sizeof(pose));check(in&&in.peek()==EOF,"saved TIN camera length");validate(pose);check(pose.origin[0]==bounds[0]&&pose.origin[1]==bounds[1],"saved TIN camera canonical basis");
   auto current=camera(owned.value,bounds);
   // The original setter reinitializes lighting/ranges even when unchanged.
   // Retain the freshly initialized model when its exact float scales match.
   if(memcmp(current.scales,pose.scales,sizeof(pose.scales)))check(sym<Scales>(module,"?SetScaleRatio@CTin3DView@@QAEFQAM@Z")(owned.value,pose.scales)>0,"restore original TIN scales");local_pose(base,owned.value,pose);
  }else{
   if(scale_flag)check(sym<Scales>(module,"?SetScaleRatio@CTin3DView@@QAEFQAM@Z")(owned.value,scales)>0,"original TIN scales");
   pose=camera(owned.value,bounds);
   if(world_flag){for(int i=0;i<3;++i){pose.eye[i]=float((eye[i]-(i<2?bounds[i]:0.))*pose.scales[i]);pose.target[i]=float((target[i]-(i<2?bounds[i]:0.))*pose.scales[i]);}pose.fov=fov;local_pose(base,owned.value,pose);}
  }
  check(sym<Fov>(module,"?SetViewFovAngle@CTin3DView@@QAEFM@Z")(owned.value,fov)>0,"original TIN field of view");auto actual=camera(owned.value,bounds);validate(actual);
  if(restored){pose.fov=fov;check(!memcmp(&pose,&actual,sizeof(pose)),"exact saved TIN camera readback");}
  bind_texture(module,owned.value,folder,bounds);
  TinLines lines;lines.bind(owned.value,folder,bounds);
   bool flying=GetFileAttributesW((folder+L"/flight.txt").c_str())!=INVALID_FILE_ATTRIBUTES;
   if(flying){actual=record_flight(folder,module,base,owned.value,width,height,bounds);}else{
  using Frame=long(__thiscall*)(void*);check(sym<Frame>(base,"?BeginRenderScene@C3DView@@UAEJXZ")(owned.value)>0,"original TIN begin frame");check(sym<Frame>(module,"?RenderScene@CTin3DView@@EAEJXZ")(owned.value)>0,"original TIN render");
  GLint viewport[4]{};glGetIntegerv(GL_VIEWPORT,viewport);check(viewport[2]==width&&viewport[3]==height,"original TIN viewport");glReadBuffer(GL_BACK);glPixelStorei(GL_PACK_ALIGNMENT,1);glPixelStorei(GL_PACK_ROW_LENGTH,0);glPixelStorei(GL_PACK_SKIP_ROWS,0);glPixelStorei(GL_PACK_SKIP_PIXELS,0);glFinish();
  std::vector<unsigned char> pixels(size_t(width)*height*3);glReadPixels(0,0,width,height,GL_RGB,GL_UNSIGNED_BYTE,pixels.data());check(glGetError()==GL_NO_ERROR,"original TIN RGB readback");native_scene::write_frame(folder+L"/scene.tiles",width,height,pixels);sym<Frame>(base,"?EndRenderScene@C3DView@@UAEJXZ")(owned.value);
   }
  auto output_name=ansi((folder+L"/camera.bin").c_str());check(GetFileAttributesA(output_name.data())==INVALID_FILE_ATTRIBUTES,"TIN camera output exists");std::ofstream out(output_name.data(),std::ios::binary);out.write(reinterpret_cast<const char*>(&actual),sizeof(actual));out.close();check(bool(out),"TIN camera saved");
  check(!IsWindowVisible(owned.window),"owned TIN window hidden");printf("{\"event\":\"tin_scene_rendered\",\"width\":%d,\"height\":%d,\"points\":%d,\"triangles\":%d,\"records_preserved\":true,\"window_visible\":false,\"camera_restored\":%s,\"renderer\":\"Microsoft GDI Generic\"}\n",width,height,points,triangles,restored?"true":"false");fflush(stdout);
  native_scene::saved_file(folder,L"model.tin");native_scene::saved_file(folder,L"camera.bin");native_scene::saved_file(folder,L"scene.tiles");if(GetFileAttributesW((folder+L"/texture.msi").c_str())!=INVALID_FILE_ATTRIBUTES)native_scene::saved_file(folder,L"texture.msi");if(GetFileAttributesW((folder+L"/lines.xyz").c_str())!=INVALID_FILE_ATTRIBUTES){native_scene::saved_file(folder,L"lines.xyz");native_scene::saved_file(folder,L"overlay.wl");}puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const std::exception& error){fprintf(stderr,"%s\n",error.what());fflush(stderr);return 1;}
}
}
