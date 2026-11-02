// Exact original DEM persistence and terrain scene rendering in this owned host.
#pragma once
#include <GL/gl.h>
#include <vfw.h>
#pragma comment(lib,"gdi32.lib")
#pragma comment(lib,"opengl32.lib")
#pragma comment(lib,"vfw32.lib")
namespace native_scene {
#pragma pack(push,1)
struct DemParameters {unsigned char type;short cell_type;double xmin,xmax,ymin,ymax,dx,dy;short flags,pyramids;double zmin,zmax;};
struct PyramidInfo {unsigned char type;double dx,dy;int rows,columns;short tile_rows,tile_columns;int block_rows,block_columns;};
struct TextureRange {double xmin,ymin,xmax,ymax,dx,dy;short columns,rows;};
struct TextureOptions {int red,green,blue;unsigned char kind,resample;unsigned short maximum;};
struct TextureInfo {unsigned char kind,channels;unsigned short width,height;unsigned bytes,gl_format;};
struct TextureCache {unsigned capacity,used,entry_bytes;void* buffer;unsigned next,last;};
#pragma pack(pop)
static_assert(sizeof(DemParameters)==71&&sizeof(PyramidInfo)==37,"native3D x86 ABI");
static_assert(sizeof(TextureRange)==52&&sizeof(TextureOptions)==16&&sizeof(TextureInfo)==14&&sizeof(TextureCache)==24,"native texture x86 ABI");
using Factory=void*(__cdecl*)();using Dataset=int(__stdcall*)(void*,void*,float);
using CreateLayer=short(__stdcall*)(void*,int,const char*,void*,int,char);
using OpenLayer=int(__stdcall*)(void*,int,const char*,int);
using LayerInfo=short(__stdcall*)(void*,int,int,void*);
using PyramidInfoFn=short(__stdcall*)(void*,int,int,short,void*);
using OpenPyramid=short(__stdcall*)(void*,int,int,short);
using ClosePyramid=short(__stdcall*)(void*,int,int);
using SetCell=short(__stdcall*)(void*,int,int,short,int,int,int,double);
using GetCell=double(__stdcall*)(void*,int,int,short,int,int,int,unsigned char*);
using CloseLayer=short(__stdcall*)(void*,int,int);using CloseDataset=short(__stdcall*)(void*,int);using DeleteAdapter=short(__stdcall*)(void*);
using Create=long(__cdecl*)(void**,void**);using Initialize=long(__cdecl*)(void*,HWND,void*);
using Release=long(__cdecl*)(void*,void*);using Prepare=long(__cdecl*)(void*,void*,void*,int,int,int);
using Reshape=long(__cdecl*)(void*,void*,long,long);using Draw=long(__cdecl*)(void*,void*);
using SetDisplay=long(__cdecl*)(void*,void*,int);
using SetCamera=long(__cdecl*)(void*,void*,const float*,const float*);
using GetEye=long(__cdecl*)(void*,void*,float*,float*,float*);
using SetRatio=long(__cdecl*)(void*,void*,float);using GetRatio=long(__cdecl*)(void*,void*,float*);
void check(bool valid,const char* message){if(!valid)throw std::runtime_error(message);}
HWND software_window(int width,int height){
 WNDCLASSW klass{};klass.style=CS_OWNDC;klass.lpfnWndProc=DefWindowProcW;klass.hInstance=GetModuleHandleW(nullptr);klass.lpszClassName=L"MapGISAgentOriginal3D";
 check(RegisterClassW(&klass)!=0,"scene private class");
 HWND window=CreateWindowExW(0,klass.lpszClassName,L"MapGISAgentOriginal3D",WS_POPUP|WS_CLIPSIBLINGS|WS_CLIPCHILDREN,0,0,width,height,nullptr,nullptr,klass.hInstance,nullptr);check(window!=nullptr,"scene private window");
 HDC dc=GetDC(window);PIXELFORMATDESCRIPTOR descriptor{};int count=DescribePixelFormat(dc,1,sizeof(descriptor),&descriptor),selected=0;
 for(int index=1;index<=count;++index){
  if(!DescribePixelFormat(dc,index,sizeof(descriptor),&descriptor))continue;
  DWORD required=PFD_DRAW_TO_WINDOW|PFD_SUPPORT_OPENGL|PFD_GENERIC_FORMAT|PFD_DOUBLEBUFFER;
  if((descriptor.dwFlags&required)==required&&!(descriptor.dwFlags&PFD_GENERIC_ACCELERATED)&&descriptor.iPixelType==PFD_TYPE_RGBA&&descriptor.cColorBits>=24&&descriptor.cDepthBits>=16){selected=index;break;}
 }
 BOOL configured=selected&&DescribePixelFormat(dc,selected,sizeof(descriptor),&descriptor)&&SetPixelFormat(dc,selected,&descriptor);ReleaseDC(window,dc);
 check(configured!=0,"scene Windows software pixel format unavailable");return window;
}
void saved_file(const std::wstring& folder,const std::wstring& relative){
 check(relative.find(L"..") == std::wstring::npos,"scene member name");auto name=ansi(relative.c_str());for(const unsigned char* c=reinterpret_cast<unsigned char*>(name.data());*c;++c)check((*c>='a'&&*c<='z')||(*c>='A'&&*c<='Z')||(*c>='0'&&*c<='9')||*c=='_'||*c=='.'||*c=='/',"scene member encoding");
 printf("{\"event\":\"scene_file\",\"name\":\"%s\",\"sha256\":\"%s\"}\n",name.data(),digest((folder+L"/"+relative).c_str()).c_str());fflush(stdout);
}
void saved_dem(const std::wstring& folder){
 saved_file(folder,L"terrain.dem");WIN32_FIND_DATAW entry{};HANDLE search=FindFirstFileW((folder+L"/terrain/*").c_str(),&entry);check(search!=INVALID_HANDLE_VALUE,"scene DEM members");int count=0;
 do{if(!wcscmp(entry.cFileName,L".")||!wcscmp(entry.cFileName,L".."))continue;check(!(entry.dwFileAttributes&(FILE_ATTRIBUTE_DIRECTORY|FILE_ATTRIBUTE_REPARSE_POINT)),"scene DEM member type");check(++count<=4096,"scene DEM member bound");saved_file(folder,L"terrain/"+std::wstring(entry.cFileName));}while(FindNextFileW(search,&entry));FindClose(search);check(count>=2,"scene DEM incomplete");
}
HMODULE texture_module(const std::wstring& program){
 auto path=program+L"/TexDbs.dll";check(digest(path.c_str())=="b6b6fa0d701c9ff939343eaf87a3b7e7e188ec4626f89205c25248cc5c363c08","scene original texture profile");
 auto module=LoadLibraryW(path.c_str());check(module!=nullptr,"scene texture module");return module;
}
TextureInfo texture_header(HMODULE module,void* adapter,const char* path){
 using Get=short(__stdcall*)(void*,const char*,void*);TextureInfo info{};
 check(sym<Get>(module,"_GetTextureDbsInfo")(adapter,path,&info)>0,"scene texture header readback");
 check(info.kind==1&&info.channels==3&&info.width>=2&&info.height>=2&&info.width<=1024&&info.height<=1024
  &&!(info.width&(info.width-1))&&!(info.height&(info.height-1))&&info.bytes==unsigned(info.width)*info.height*3&&!info.gl_format,"scene uncompressed RGB texture profile");return info;
}
void bind_texture(const std::wstring& program,const std::wstring& folder,HMODULE renderer,void* drawing,void* state,const DemParameters& dem,int cols,int rows){
 auto database=ansi((folder+L"/texture.tdb").c_str()),image=ansi((folder+L"/texture.msi").c_str());
 if(GetFileAttributesA(database.data())==INVALID_FILE_ATTRIBUTES&&GetFileAttributesA(image.data())==INVALID_FILE_ATTRIBUTES)return;
 HMODULE module=texture_module(program);void* adapter=sym<Factory>(module,"_CreateTexDbsAI")();check(adapter!=nullptr,"scene texture adapter");
 if(GetFileAttributesA(database.data())==INVALID_FILE_ATTRIBUTES){
  GLint maximum=0;glGetIntegerv(GL_MAX_TEXTURE_SIZE,&maximum);check(maximum>=1024,"scene software texture capacity requires1024");
  // One base tile spans the actual terrain extent. A fixed257-cell domain
  // stretches small source images over unused terrain; it changes their map.
  TextureRange range{dem.xmin,dem.ymin,dem.xmax,dem.ymax,dem.dx,dem.dy,short(cols),short(rows)};
  TextureOptions options{0,1,2,1,0,2048};using CreateTexture=short(__stdcall*)(void*,const char*,void*,void*,const char*);
  check(sym<CreateTexture>(module,"_CreateTextureDbs")(adapter,image.data(),&range,&options,database.data())>0,"scene original texture create");
 }
 auto info=texture_header(module,adapter,database.data());GLint supported=0;glGetIntegerv(GL_MAX_TEXTURE_SIZE,&supported);check(info.width<=supported&&info.height<=supported,"scene texture OpenGL capacity");check(sym<DeleteAdapter>(module,"_DeleteTexDbsAI")(adapter)>0,"scene texture adapter release");
 using LoadTexture=long(__cdecl*)(void*,void*,const char*);
 check(sym<LoadTexture>(renderer,"_tnPrepareTexture")(drawing,state,database.data())==0,"scene original texture bind");
 check(sym<SetDisplay>(renderer,"_SetSurfaceDispMode")(drawing,state,0)==0,"scene textured surface");
 // The pinned loader ignores allocation failure for its physical-memory-sized
 // ring. Use its constructor/destructor/allocator on this owned instance;
 // never change an original DLL, IAT, other process or system memory policy.
 auto owned=*reinterpret_cast<unsigned char**>(reinterpret_cast<unsigned char*>(renderer)+0x36454);check(owned!=nullptr,"scene owned texture adapter");
 auto stream=*reinterpret_cast<unsigned char**>(owned);check(stream!=nullptr,"scene owned texture stream");auto ring=stream+374;
 TextureCache before{},after{};memcpy(&before,ring,sizeof(before));unsigned tiles=0;memcpy(&tiles,static_cast<unsigned char*>(state)+1064985,4);
 check(before.used==0&&before.entry_bytes==info.bytes+4&&tiles>=1&&tiles<=2,"scene unused original texture cache");
 unsigned capacity=std::min(tiles,(8u*1024*1024)/before.entry_bytes);check(capacity>0,"scene texture cache budget");
 using RingRelease=int(__thiscall*)(void*);using RingCreate=void*(__thiscall*)(void*);using RingSize=int(__thiscall*)(void*,unsigned,unsigned);
 auto base=reinterpret_cast<unsigned char*>(module);
 if(before.buffer)reinterpret_cast<RingRelease>(base+0x5b30)(ring);
 reinterpret_cast<RingCreate>(base+0x5b10)(ring);
 check(reinterpret_cast<RingSize>(base+0x5b40)(ring,capacity,before.entry_bytes)!=0,"scene owned texture cache allocation");memcpy(&after,ring,sizeof(after));
 check(after.capacity==capacity&&!after.used&&after.entry_bytes==before.entry_bytes&&after.buffer&&!after.next&&!after.last,"scene texture cache readback");
 printf("{\"event\":\"scene_texture_bound\",\"width\":%u,\"height\":%u,\"tiles\":%u,\"cache_capacity\":%u,\"cache_bytes\":%u,\"initial_native_cache_request\":%llu,\"initial_native_cache_allocated\":%s}\n",info.width,info.height,tiles,capacity,capacity*after.entry_bytes,static_cast<unsigned long long>(before.capacity)*before.entry_bytes,before.buffer?"true":"false");fflush(stdout);
}
void saved_texture(const std::wstring& program,const std::wstring& folder){
 auto database=ansi((folder+L"/texture.tdb").c_str());if(GetFileAttributesA(database.data())==INVALID_FILE_ATTRIBUTES)return;
 HMODULE module=texture_module(program);void* adapter=sym<Factory>(module,"_CreateTexDbsAI")();check(adapter!=nullptr,"scene texture inspect adapter");
 auto info=texture_header(module,adapter,database.data());check(sym<DeleteAdapter>(module,"_DeleteTexDbsAI")(adapter)>0,"scene texture inspect release");
 printf("{\"event\":\"scene_texture_header\",\"width\":%u,\"height\":%u,\"bytes\":%u}\n",info.width,info.height,info.bytes);fflush(stdout);
 saved_file(folder,L"texture.tdb");if(GetFileAttributesW((folder+L"/texture.msi").c_str())!=INVALID_FILE_ATTRIBUTES)saved_file(folder,L"texture.msi");
 WIN32_FIND_DATAW entry{};HANDLE search=FindFirstFileW((folder+L"/texture_DAT/*").c_str(),&entry);check(search!=INVALID_HANDLE_VALUE,"scene texture data directory");int count=0;
 do{if(!wcscmp(entry.cFileName,L".")||!wcscmp(entry.cFileName,L".."))continue;check(!(entry.dwFileAttributes&(FILE_ATTRIBUTE_DIRECTORY|FILE_ATTRIBUTE_REPARSE_POINT))&&++count==1&&!wcscmp(entry.cFileName,L"Dat0.py"),"scene native texture data member");saved_file(folder,L"texture_DAT/"+std::wstring(entry.cFileName));}while(FindNextFileW(search,&entry));FindClose(search);check(count==1,"scene texture data missing");
}
#include "NativeAnnotations.h"
#include "NativeRegions.h"

std::vector<unsigned char> frame_rgb(HMODULE module,void* drawing,void* state,int width,int height){
 auto flag=static_cast<unsigned char*>(drawing)+76;check(*flag==0,"owned export flag");
 struct Export{unsigned char* flag;~Export(){*flag=0;}} owned{flag};*flag=1;
 using NativeDraw=long(__thiscall*)(void*,void*);
 AnnotationBaseDraw annotation_base(drawing);
 RegionBaseDraw region_base(drawing);
 check(reinterpret_cast<NativeDraw>(reinterpret_cast<unsigned char*>(module)+0x13560)(drawing,state)==0,"original export draw");
 annotation_base.restore();region_base.restore();if(region_base.context)region_base.context->after(module,state);if(annotation_base.context)annotation_base.context->after(module,state);
 puts("{\"event\":\"native_export_draw_complete\"}");fflush(stdout);
 GLenum prior=glGetError();GLint viewport[4]{};glGetIntegerv(GL_VIEWPORT,viewport);check(viewport[2]==width&&viewport[3]==height,"frame viewport");
 glReadBuffer(GL_BACK);glPixelStorei(GL_PACK_ALIGNMENT,1);glPixelStorei(GL_PACK_ROW_LENGTH,0);glPixelStorei(GL_PACK_SKIP_ROWS,0);glPixelStorei(GL_PACK_SKIP_PIXELS,0);
 std::vector<unsigned char> pixels(size_t(width)*height*3);glFinish();glReadPixels(0,0,width,height,GL_RGB,GL_UNSIGNED_BYTE,pixels.data());check(glGetError()==GL_NO_ERROR,"frame RGB readback");
 printf("{\"event\":\"frame_captured\",\"prior_gl_error\":%u}\n",prior);fflush(stdout);return pixels;
}
void write_frame(const std::wstring& path,int width,int height,const std::vector<unsigned char>& pixels){
 check(pixels.size()==size_t(width)*height*3,"frame pixel count");
 auto name=ansi(path.c_str());check(GetFileAttributesW(path.c_str())==INVALID_FILE_ATTRIBUTES,"frame output exists");std::ofstream out(name.data(),std::ios::binary);check(bool(out),"frame transport open");
 const char magic[8]={'M','G','R','T','0','0','0','2'};RasterTiles header{0,unsigned(width),unsigned(height),3,128,128};unsigned nodata=0;double marker=0;
 out.write(magic,8);out.write(reinterpret_cast<const char*>(&header),sizeof(header));out.write(reinterpret_cast<const char*>(&nodata),4);out.write(reinterpret_cast<const char*>(&marker),8);
 std::vector<unsigned char> tile(16384);for(int band=0;band<3;++band)for(int row=0;row<(height+127)/128;++row)for(int col=0;col<(width+127)/128;++col){
  std::fill(tile.begin(),tile.end(),0);for(int y=0;y<128&&row*128+y<height;++y)for(int x=0;x<128&&col*128+x<width;++x)tile[size_t(y)*128+x]=pixels[(size_t(height-1-row*128-y)*width+col*128+x)*3+band];out.write(reinterpret_cast<const char*>(tile.data()),tile.size());}
 out.flush();check(bool(out),"frame transport write");out.close();puts("{\"event\":\"frame_written\"}");fflush(stdout);
}
void frame_output(HMODULE module,void* drawing,void* state,const std::wstring& path,int width,int height){write_frame(path,width,height,frame_rgb(module,drawing,state,width,height));}

#include "NativeCamera.h"
#include "NativeFlight.h"

bool open_lines(const std::wstring& folder,Session& session,long& active,long& upper){
 auto file=folder+L"/overlay.wl";if(GetFileAttributesW(file.c_str())==INVALID_FILE_ATTRIBUTES)return false;
 session.library=GetModuleHandleW(L"bas60.dll");check(session.library!=nullptr,"scene line BAS60");load_area(session,0,file);
 check(sym<Count>(session.library,"?_GetLinNum@C60_WorkArea@@QAEFPAJ0@Z")(&session.area,&active,&upper)>0&&active>=1&&active<=20000&&upper>active,"scene native WL count");return true;
}
void bind_lines(const std::wstring& folder,HMODULE module,void* drawing,void* state,Session& session){
 long active=0,upper=0;if(!open_lines(folder,session,active,upper))return;
 using LoadLine=long(__cdecl*)(void*,void*,int,int,float);
 check(sym<LoadLine>(module,"_tnLoadMapLine")(drawing,state,session.area.area,0x8000,0.f)==0,"scene original WL drape");
 printf("{\"event\":\"scene_lines_bound\",\"active\":%ld,\"record_upper_bound\":%ld,\"flags\":32768,\"reduce_tolerance\":0}\n",active,upper);fflush(stdout);
}
void saved_lines(const std::wstring& folder){
 Session session;long active=0,upper=0;if(!open_lines(folder,session,active,upper))return;
 printf("{\"event\":\"scene_lines_readback\",\"active\":%ld,\"record_upper_bound\":%ld}\n",active,upper);fflush(stdout);session.close();saved_file(folder,L"overlay.wl");
}

int run(wchar_t** argv){
 SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
 try{
  std::wstring mode=argv[2],program=argv[3],folder=argv[5];check(mode==L"create"||mode==L"render"||mode==L"flight"||mode==L"inspect","scene operation");
  auto input_name=ansi(argv[4]),output_name=ansi(argv[5]);check(strlen(output_name.data())<128,"scene short native alias required");check(SetCurrentDirectoryW(folder.c_str())!=0,"scene owned working directory");
  const std::pair<const wchar_t*,const char*> modules[]={
   {L"bas60.dll",BAS_SHA},{L"basroot.dll","d1109d09d3708911a68a490fc86d64e8fbf30b3f53a7ffce8de2241ec5cc36ba"},
   {L"mapdsp60.dll","e1995ea477c7d87bd6ed75d7327376b9a5f9ed8d2564b0a42dbada9197b671cb"},
   {L"DemArea.dll","384332e2db3690dff959e4b38e5f42fc4b8d683531c1f44570a913da6e6803c2"},
   {L"NewTin3DFly.dll","c4c1c342e0d45a2008cb01fe3b274fdab5cf393d63455ab5226d9c69c73e9340"},
   {L"Dem3DDlg.dll","9fd2f6cccb3c44623edf4c86d00bdbe53a4f7fca41c7cecd31ee8442761a29f4"},
   {L"DemDlg.dll","07c7038e6f6f0cfe416a9b44ca99c14bc46ea2d67646d89a15e2e7ab27eaa0c1"},
   {L"TexDbs.dll","b6b6fa0d701c9ff939343eaf87a3b7e7e188ec4626f89205c25248cc5c363c08"}};
  for(const auto& m:modules)check(digest((program+L"/"+m.first).c_str())==m.second,"scene original DLL profile mismatch");SetDllDirectoryW(program.c_str());
  DWORD dep=0;BOOL permanent=FALSE;check(GetProcessDEPPolicy(GetCurrentProcess(),&dep,&permanent)&&dep==0,"scene isolated legacy compatibility");auto desktop=desktop_name();check(desktop.rfind("MapGISAgent_",0)==0,"scene private desktop required");
  printf("{\"event\":\"host\",\"pid\":%lu,\"bits\":32,\"dep_flags\":%lu,\"desktop\":\"%s\"}\n",GetCurrentProcessId(),dep,desktop.c_str());fflush(stdout);
  std::ifstream input(input_name.data(),std::ios::binary);int cols=0,rows=0;double bounds[4]{};input.read(reinterpret_cast<char*>(&cols),4);input.read(reinterpret_cast<char*>(&rows),4);input.read(reinterpret_cast<char*>(bounds),32);
  check(input&&cols>=2&&rows>=2&&cols<=16384&&rows<=16384&&static_cast<long long>(cols)*rows<=2000000,"scene DEM dimensions");
  for(double v:bounds)check(std::isfinite(v),"scene finite bounds");check(bounds[0]<bounds[2]&&bounds[1]<bounds[3],"scene DEM bounds");
  std::vector<float> values(size_t(cols)*rows);input.read(reinterpret_cast<char*>(values.data()),values.size()*4);check(input&&input.peek()==EOF,"scene height transport");float low=values[0],high=values[0];for(float z:values){check(std::isfinite(z)&&fabs(z)<1e18,"scene finite native height");low=(std::min)(low,z);high=(std::max)(high,z);}
  HMODULE dem_module=LoadLibraryW((program+L"/DemArea.dll").c_str());check(dem_module!=nullptr,"scene DEM module load");void* adapter=sym<Factory>(dem_module,"_dmCreateDemBaseAI")();check(adapter!=nullptr,"scene DEM adapter");int dataset=sym<Dataset>(dem_module,"_dmOpenDataSet")(adapter,nullptr,.125f);check(dataset>0,"scene DEM dataset");
  DemParameters expected{};expected.cell_type=7;expected.xmin=bounds[0];expected.ymin=bounds[1];expected.xmax=bounds[2];expected.ymax=bounds[3];expected.dx=(bounds[2]-bounds[0])/(cols-1);expected.dy=(bounds[3]-bounds[1])/(rows-1);expected.zmin=low;expected.zmax=high;
  std::wstring dem=folder+L"/terrain.dem";auto dem_name=ansi(dem.c_str());
  if(mode==L"create"){
   check(GetFileAttributesW(dem.c_str())==INVALID_FILE_ATTRIBUTES,"scene DEM output exists");check(sym<CreateLayer>(dem_module,"_dmCreateDataLayer")(adapter,dataset,dem_name.data(),&expected,0,0)>0,"scene native DEM create");int layer=sym<OpenLayer>(dem_module,"_dmOpenDataLayer")(adapter,dataset,dem_name.data(),1);check(layer>0,"scene native DEM writable open");check(sym<OpenPyramid>(dem_module,"_dmOpenPyramidLayer")(adapter,dataset,layer,1)>0,"scene native pyramid open");auto set=sym<SetCell>(dem_module,"_dmSetCell");for(int r=0;r<rows;++r)for(int c=0;c<cols;++c)check(set(adapter,dataset,layer,1,0,r,c,values[size_t(r)*cols+c])>0,"scene native height write");check(sym<ClosePyramid>(dem_module,"_dmClosePyramidLayer")(adapter,dataset,layer)>0,"scene native height flush");check(sym<CloseLayer>(dem_module,"_dmCloseDataLayer")(adapter,dataset,layer)>0,"scene native DEM save");
  }
  int layer=sym<OpenLayer>(dem_module,"_dmOpenDataLayer")(adapter,dataset,dem_name.data(),0);check(layer>0,"scene native DEM reopen");DemParameters actual{};PyramidInfo pyramid{};
  check(sym<LayerInfo>(dem_module,"_dmGetDataLayerParam")(adapter,dataset,layer,&actual)>0,"scene DEM parameters");check(sym<PyramidInfoFn>(dem_module,"_dmGetPyramidLayerInfo")(adapter,dataset,layer,1,&pyramid)>0&&pyramid.rows==rows&&pyramid.columns==cols,"scene DEM shape readback");
  check(actual.cell_type==7&&actual.xmin==expected.xmin&&actual.xmax==expected.xmax&&actual.ymin==expected.ymin&&actual.ymax==expected.ymax&&actual.dx==expected.dx&&actual.dy==expected.dy&&actual.zmin==expected.zmin&&actual.zmax==expected.zmax,"scene DEM parameter readback");
  check(sym<OpenPyramid>(dem_module,"_dmOpenPyramidLayer")(adapter,dataset,layer,1)>0,"scene read pyramid open");auto get=sym<GetCell>(dem_module,"_dmGetCell");for(int r=0;r<rows;++r)for(int c=0;c<cols;++c){unsigned char missing=1;double z=get(adapter,dataset,layer,1,0,r,c,&missing);check(!missing&&z==values[size_t(r)*cols+c],"scene native elevation readback");}check(sym<ClosePyramid>(dem_module,"_dmClosePyramidLayer")(adapter,dataset,layer)>0,"scene read pyramid close");
  printf("{\"event\":\"scene_dem_readback\",\"rows\":%d,\"columns\":%d,\"cells\":%zu,\"bounds\":[%.17g,%.17g,%.17g,%.17g],\"range\":[%.17g,%.17g],\"all_cells_equal\":true}\n",rows,cols,values.size(),actual.xmin,actual.ymin,actual.xmax,actual.ymax,actual.zmin,actual.zmax);fflush(stdout);
  if(mode!=L"inspect"){
   int width=0,height=0,display=0,camera=0;float quality=0,eye[3]{},target[3]{};auto settings_name=ansi(argv[6]);std::ifstream settings(settings_name.data());settings.imbue(std::locale::classic());settings>>width>>height>>display>>camera>>quality>>eye[0]>>eye[1]>>eye[2]>>target[0]>>target[1]>>target[2];std::string extra;check(settings&&!(settings>>extra)&&width>=32&&height>=32&&width<=2048&&height<=2048&&(display==0||display==1)&&(camera==0||camera==1)&&std::isfinite(quality)&&quality>=.01f&&quality<=1.f,"scene render parameters");for(float v:eye)check(std::isfinite(v),"scene eye");for(float v:target)check(std::isfinite(v),"scene target");
   auto msi=folder+L"/scene.msi";check(GetFileAttributesW(msi.c_str())==INVALID_FILE_ATTRIBUTES,"scene MSI output exists");HMODULE module=LoadLibraryW((program+L"/NewTin3DFly.dll").c_str());check(module!=nullptr,"scene original renderer load");terrain_temp_folder(msi.c_str());HWND window=software_window(width,height);void* drawing=nullptr;void* state=nullptr;check(sym<Create>(module,"_tnCreateTin3DDraw")(&drawing,&state)==0&&drawing&&state,"scene original objects");check(sym<Initialize>(module,"_tnInitial3DScene")(drawing,window,state)==0,"scene original OpenGL initialize");
   const char* vendor=reinterpret_cast<const char*>(glGetString(GL_VENDOR));const char* renderer=reinterpret_cast<const char*>(glGetString(GL_RENDERER));check(vendor&&renderer&&!strcmp(renderer,"GDI Generic")&&wglGetCurrentContext(),"scene software context readback");
   check(std::min(rows,cols)*double(quality)>=2.0,"scene sampling must retain two rows and columns");
   check(sym<Prepare>(module,"_tnPrepare3DScene")(drawing,state,adapter,dataset,layer,1)==0,"scene original model prepare");
   // Keep the original renderer at the explicit requested sampling ratio;
   // the default half sampling collapses a two-by-two source grid.
   check(sym<SetDisplay>(module,"_SetDispMethod")(drawing,state,0)==0,"scene overview for sampling");
   check(sym<SetRatio>(module,"_SetResampleRatio")(drawing,state,quality)==0,"scene native sampling");float actual_sampling=0;
   check(sym<GetRatio>(module,"_GetResampleRatio")(drawing,state,&actual_sampling)==0&&actual_sampling==quality,"scene sampling readback");
   Session line_session;bind_lines(folder,module,drawing,state,line_session);
   bind_texture(program,folder,module,drawing,state,expected,cols,rows);
   NativeRegions regions;regions.bind(folder,module,drawing,state,expected);
   NativeAnnotations annotations;annotations.bind(folder,module,drawing,state);
   check(sym<SetDisplay>(module,"_SetDispMethod")(drawing,state,display)==0,"scene display method");if(camera)check(sym<SetCamera>(module,"_SetEyeCenterPosition")(drawing,state,eye,target)==0,"scene original camera");
   bool camera_restored=restore_camera(folder,module,drawing,state,display);
   float actual_eye[3]{};check(sym<GetEye>(module,"_GetEyeParam")(drawing,state,actual_eye,actual_eye+1,actual_eye+2)==0,"scene eye readback");for(float v:actual_eye)check(std::isfinite(v),"scene finite effective camera");check(sym<Reshape>(module,"_tnReshape")(drawing,state,width,height)==0,"scene original viewport");
   {AnnotationBaseDraw annotation_base(drawing);RegionBaseDraw region_base(drawing);check(sym<Draw>(module,"_tnDrawScene")(drawing,state)==0,"scene original draw");}
   auto fly_limits=flight_speed_range(module,drawing,state);
   if(mode==L"flight"){check(display==1,"flight requires walk mode");auto pose=record_flight(folder,module,drawing,state,expected,width,height);memcpy(actual_eye,pose.world_eye,sizeof(actual_eye));}
   else frame_output(module,drawing,state,folder+L"/scene.tiles",width,height);
   if(camera_restored&&mode!=L"flight")camera_event(read_camera(module,drawing,state),"scene_camera");
   check(sym<Release>(module,"_tnRelease3DScene")(drawing,state)==0,"scene context release");check(sym<Release>(module,"_tnDeleteTin3DDraw")(drawing,state)==0,"scene object release");check(!IsWindowVisible(window),"scene hidden window");DestroyWindow(window);UnregisterClassW(L"MapGISAgentOriginal3D",GetModuleHandleW(nullptr));
   printf("{\"event\":\"scene_rendered\",\"width\":%d,\"height\":%d,\"mode\":%d,\"sampling_ratio\":%.9g,\"eye\":[%.9g,%.9g,%.9g],\"fly_speed_range\":[%.9g,%.9g],\"renderer\":\"Microsoft GDI Generic\",\"window_visible\":false}\n",width,height,display,actual_sampling,actual_eye[0],actual_eye[1],actual_eye[2],fly_limits.first,fly_limits.second);fflush(stdout);saved_file(folder,L"scene.tiles");
  }
  if(mode==L"flight")saved_file(folder,L"flight.avi");else if(mode==L"inspect")inspect_flight(folder);
  check(sym<CloseLayer>(dem_module,"_dmCloseDataLayer")(adapter,dataset,layer)>0,"scene DEM close");check(sym<CloseDataset>(dem_module,"_dmCloseDataSet")(adapter,dataset)>0,"scene dataset close");check(sym<DeleteAdapter>(dem_module,"_dmDeleteDemBaseAI")(adapter)>0,"scene adapter release");saved_dem(folder);saved_texture(program,folder);saved_lines(folder);saved_notes(folder);saved_regions(folder);puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const std::exception& error){fprintf(stderr,"%s\n",error.what());fflush(stderr);return 1;}
}
}
