// Exact-build BAS60 worker. Own process only; never attaches to an editor.
// C60_WorkArea has pure virtual coordinate converters. Geometry uses the verified
// ordinal API instead. Every operation is staged and independently checked by Python.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wincrypt.h>
#include <stdio.h>
#include <string>
#include <vector>
#include <fstream>
#include <stdexcept>
#include <cmath>
#include <sstream>
#include <set>
#include <locale>
#include <iostream>
#include "PrivateDesktop.h"
#pragma comment(lib,"advapi32.lib")
#pragma comment(lib,"user32.lib")
static const char* BAS_SHA="680029d3ed7a04338a6799f1e16cab30d6e421d13e3d8c66bbeccb057a50ee24";
std::string desktop_name(){char name[128]{};DWORD bytes=0;if(!GetUserObjectInformationA(GetThreadDesktop(GetCurrentThreadId()),UOI_NAME,name,sizeof(name),&bytes))throw std::runtime_error("desktop identity");return name;}
struct Area {void* vtable;short in_format,out_format;void* owned;short descriptor,area;unsigned char open,pad[3];};
static_assert(sizeof(Area)==20,"verified x86 layout");
template<class T>T sym(HMODULE h,const char* n){auto p=GetProcAddress(h,n);if(!p)throw std::runtime_error("missing native export");return reinterpret_cast<T>(p);}
std::vector<char> ansi(const wchar_t* s){BOOL bad=FALSE;int n=WideCharToMultiByte(936,WC_NO_BEST_FIT_CHARS,s,-1,nullptr,0,nullptr,&bad);if(!n||bad||n>240)throw std::runtime_error("native CP936 path limit");std::vector<char>a(MAX_PATH,0);WideCharToMultiByte(936,WC_NO_BEST_FIT_CHARS,s,-1,a.data(),n,nullptr,&bad);if(bad)throw std::runtime_error("native CP936 path loss");return a;}
std::string digest(const wchar_t* path){
 HCRYPTPROV p=0;HCRYPTHASH h=0;HANDLE f=CreateFileW(path,GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,0,nullptr);
 if(f==INVALID_HANDLE_VALUE)throw std::runtime_error("native DLL unavailable");
 bool ok=CryptAcquireContextW(&p,nullptr,nullptr,PROV_RSA_AES,CRYPT_VERIFYCONTEXT)&&CryptCreateHash(p,CALG_SHA_256,0,0,&h);
 BYTE block[65536],out[32];DWORD count=0,size=32;
 while(ok){if(!ReadFile(f,block,sizeof block,&count,nullptr)){ok=false;break;}if(!count)break;ok=CryptHashData(h,block,count,0)!=FALSE;}
 if(ok)ok=CryptGetHashParam(h,HP_HASHVAL,out,&size,0)!=FALSE;
 CloseHandle(f);if(h)CryptDestroyHash(h);if(p)CryptReleaseContext(p,0);if(!ok)throw std::runtime_error("native DLL hash failed");
 std::string result;for(BYTE b:out){char x[3];sprintf_s(x,"%02x",b);result+=x;}return result;
}
using Life=void(__thiscall*)(Area*);
using Open=short(__thiscall*)(Area*,short,short,short);
using File=short(__thiscall*)(Area*,char*,char*,char*,char*);
using Count=short(__thiscall*)(Area*,long*,long*);
using RawPos=short(__stdcall*)(int,long,void*,int);
struct Session {HMODULE library=nullptr;Area area{};Life destroy=nullptr;bool constructed=false;
 ~Session(){if(constructed)destroy(&area);if(library)FreeLibrary(library);}
};
void require(short result,const char* what){if(result<=0)throw std::runtime_error(what);}
void counts(Area* a,Count fn,const char* stage){long active=-1,upper=-1;require(fn(a,&active,&upper),"count failed");printf("{\"event\":\"count\",\"stage\":\"%s\",\"active\":%ld,\"record_upper_bound\":%ld}\n",stage,active,upper);fflush(stdout);}

// The persistent worker owns its native work area and communicates only over
// its inherited pipes. Checkpoints are recovery copies, never the user's input.
void edit_area(Session& s,int kind,const std::wstring& op,std::istream& stream){
 struct Move {long id;double xy[2];};std::vector<Move> moves;std::vector<long> ids;std::set<long> unique;
 if(op==L"point.move"){
  if(kind!=1)throw std::runtime_error("point operation requires WT");Move m{};
  while(stream>>m.id>>m.xy[0]>>m.xy[1]){if(moves.size()>=1000||m.id<1||!std::isfinite(m.xy[0])||!std::isfinite(m.xy[1])||!unique.insert(m.id).second)throw std::runtime_error("point arguments");moves.push_back(m);}
  if(moves.empty()||!stream.eof())throw std::runtime_error("invalid point data");
  auto get=sym<RawPos>(s.library,MAKEINTRESOURCEA(1558));auto set=sym<RawPos>(s.library,MAKEINTRESOURCEA(1561));
  for(auto& m:moves){double xy[2];require(get(s.area.area,m.id,xy,2),"active point required");}
  for(auto& m:moves){double xy[2];require(set(s.area.area,m.id,m.xy,2),"point update failed");require(get(s.area.area,m.id,xy,2),"point readback failed");if(xy[0]!=m.xy[0]||xy[1]!=m.xy[1])throw std::runtime_error("point readback differs");}
 }else if(op==L"feature.delete"){
  long id;while(stream>>id){if(ids.size()>=1000||id<1||!unique.insert(id).second)throw std::runtime_error("delete IDs");ids.push_back(id);}if(ids.empty()||!stream.eof())throw std::runtime_error("invalid delete data");
  using Del=short(__thiscall*)(Area*,long);auto del=sym<Del>(s.library,kind==2?"?_DelReg@C60_WorkArea@@QAEFJ@Z":kind==1?"?_DelPnt@C60_WorkArea@@QAEFJ@Z":"?_DelLin@C60_WorkArea@@QAEFJ@Z");for(long id:ids)require(del(&s.area,id),"native deletion failed");
 }else throw std::runtime_error("unsupported edit");
}

void load_area(Session& s,int kind,const std::wstring& path){
 if(s.constructed){s.destroy(&s.area);s.constructed=false;}s.area={};
 auto ctor=sym<Life>(s.library,"??0C60_WorkArea@@QAE@XZ");s.destroy=sym<Life>(s.library,"??1C60_WorkArea@@UAE@XZ");auto open=sym<Open>(s.library,"?_OpenArea@C60_WorkArea@@QAEFFFF@Z");auto load=sym<File>(s.library,"?_LoadAFile@C60_WorkArea@@QAEFPAD000@Z");
 ctor(&s.area);s.constructed=true;require(open(&s.area,0,short(kind),0x4460),"open failed");auto in=ansi(path.c_str());require(load(&s.area,in.data(),nullptr,nullptr,nullptr),"read failed");
}

void durable_receipt(const std::wstring& path,const std::string& body){
 auto temp=path+L".tmp";HANDLE f=CreateFileW(temp.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,0,nullptr);if(f==INVALID_HANDLE_VALUE)throw std::runtime_error("checkpoint receipt create");DWORD written=0;bool ok=WriteFile(f,body.data(),DWORD(body.size()),&written,nullptr)&&written==body.size()&&FlushFileBuffers(f);CloseHandle(f);if(!ok||!MoveFileExW(temp.c_str(),path.c_str(),MOVEFILE_WRITE_THROUGH))throw std::runtime_error("checkpoint receipt flush");
}

int workspace_main(wchar_t** argv){
 SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
 try{
  int kind=_wtoi(argv[4]);if(kind<0||kind>2||digest(argv[2])!=BAS_SHA)throw std::runtime_error("workspace native profile");
  unsigned long long revision=_wcstoui64(argv[6],nullptr,10);std::wstring dll(argv[2]),current(argv[3]),base(argv[5]);
  std::wstring filename=current.substr(current.find_last_of(L"/\\")+1);if(filename.empty())throw std::runtime_error("workspace filename");
  SetDllDirectoryW(dll.substr(0,dll.find_last_of(L"/\\")).c_str());Session s;s.library=LoadLibraryW(dll.c_str());if(!s.library)throw std::runtime_error("workspace initialization");
  DWORD dep=0;BOOL permanent=FALSE;if(!GetProcessDEPPolicy(GetCurrentProcess(),&dep,&permanent)||dep)throw std::runtime_error("legacy DEP compatibility unavailable");load_area(s,kind,current);
  auto count=sym<Count>(s.library,kind==0?"?_GetLinNum@C60_WorkArea@@QAEFPAJ0@Z":kind==1?"?_GetPntNum@C60_WorkArea@@QAEFPAJ0@Z":"?_GetRegNum@C60_WorkArea@@QAEFPAJ0@Z");
  long active=0,upper=0;require(count(&s.area,&active,&upper),"workspace count");
  printf("{\"event\":\"ready\",\"pid\":%lu,\"bits\":32,\"dep_flags\":%lu,\"desktop\":\"%s\",\"revision\":%llu,\"active\":%ld}\n",GetCurrentProcessId(),dep,desktop_name().c_str(),revision,active);fflush(stdout);
  std::string line;while(std::getline(std::cin,line)){
   std::istringstream request(line);request.imbue(std::locale::classic());std::string token,op;unsigned long long expected=0;
   if(!(request>>token>>expected>>op)||token.size()!=32||token.find_first_not_of("0123456789abcdef")!=std::string::npos)throw std::runtime_error("workspace protocol");
   try{
    if(expected!=revision)throw std::runtime_error("workspace revision conflict");
    if(op=="status"||op=="close"){
     require(count(&s.area,&active,&upper),"workspace count");printf("{\"event\":\"ok\",\"request\":\"%s\",\"pid\":%lu,\"revision\":%llu,\"active\":%ld,\"record_upper_bound\":%ld}\n",token.c_str(),GetCurrentProcessId(),revision,active,upper);fflush(stdout);if(op=="close")break;continue;
    }
    std::wstring nativeOp(op.begin(),op.end());edit_area(s,kind,nativeOp,request);
    std::wstring folder=base+L"/"+std::wstring(token.begin(),token.end());if(!CreateDirectoryW(folder.c_str(),nullptr)&&GetLastError()!=ERROR_ALREADY_EXISTS)throw std::runtime_error("checkpoint directory");
    auto output=folder+L"/"+filename;if(GetFileAttributesW(output.c_str())!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("checkpoint already exists");
    auto out=ansi(output.c_str());auto save=sym<File>(s.library,"?_SaveAFile@C60_WorkArea@@QAEFPAD000@Z");require(save(&s.area,out.data(),nullptr,nullptr,nullptr),"checkpoint save");require(count(&s.area,&active,&upper),"workspace count");
    std::ostringstream response;response<<"{\"event\":\"ok\",\"request\":\""<<token<<"\",\"pid\":"<<GetCurrentProcessId()<<",\"bits\":32,\"dep_flags\":0,\"revision\":"<<(revision+1)<<",\"active\":"<<active<<",\"record_upper_bound\":"<<upper<<",\"checkpoint_complete\":true}";
    durable_receipt(folder+L"/native-complete.json",response.str());current=output;++revision;puts(response.str().c_str());fflush(stdout);
   }catch(const std::exception& e){
    // Reload the last complete checkpoint if a batch failed part way through.
    load_area(s,kind,current);fprintf(stderr,"workspace command: %s\n",e.what());printf("{\"event\":\"error\",\"request\":\"%s\",\"revision\":%llu}\n",token.c_str(),revision);fflush(stderr);fflush(stdout);
   }
  }
  return 0;
 }catch(const std::exception& e){fprintf(stderr,"%s\n",e.what());return 1;}
}

struct RectD {double xmin,ymin,xmax,ymax;};
using DrawingDrop=void(__stdcall*)(void*);
struct Drawing {void* dc=nullptr;DrawingDrop destroy=nullptr;~Drawing(){if(dc)destroy(dc);}};
void begin_eps(Drawing& drawing,HMODULE renderer,HMODULE dsp,const wchar_t* viewPath,const wchar_t* outputPath){
 if(GetFileAttributesW(outputPath)!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("plot output exists");
 auto viewFile=ansi(viewPath);std::ifstream view(viewFile.data());RectD box{};double width=0,height=0;
 if(!(view>>box.xmin>>box.ymin>>box.xmax>>box.ymax>>width>>height)||!std::isfinite(box.xmin)||!std::isfinite(box.ymin)||!std::isfinite(box.xmax)||!std::isfinite(box.ymax)||!(box.xmax>box.xmin)||!(box.ymax>box.ymin)||!(width>=1&&width<=2000&&height>=1&&height<=2000))throw std::runtime_error("plot view");
 using Make=void*(__stdcall*)(char*);using SetRange=void(__thiscall*)(void*,RectD);using Clip=short(__thiscall*)(void*,void*);using Transform=short(__stdcall*)(void*,double,double,double,double,double);
 auto make=sym<Make>(renderer,"?_GetEPSDC@@YGPAVKernelDriver@@PAD@Z");drawing.destroy=sym<DrawingDrop>(dsp,"_DeleteMyDC");auto out=ansi(outputPath);drawing.dc=make(out.data());if(!drawing.dc)throw std::runtime_error("create EPS");
 auto range=sym<SetRange>(renderer,"?set_range@HDEPSDriver@@UAEXUD_RECT@@@Z");auto clip=sym<Clip>(dsp,"?_SetDispRect@KernelDriver@@QAEFPAUD_RECT@@@Z");auto transform=sym<Transform>(dsp,"_SetDispParam");range(drawing.dc,RectD{0,0,width,height});require(clip(drawing.dc,&box),"clip");double sx=width/(box.xmax-box.xmin),sy=height/(box.ymax-box.ymin),scale=sx<sy?sx:sy;double originX=box.xmin-(width-(box.xmax-box.xmin)*scale)/(2*scale),originY=box.ymin-(height-(box.ymax-box.ymin)*scale)/(2*scale);require(transform(drawing.dc,originX,originY,scale,scale,0),"plot transform");
}
int plot_main(int argc,wchar_t**argv){
 SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
 try{
  std::wstring program(argv[2]);auto bas=program+L"/bas60.dll",dspPath=program+L"/mapdsp60.dll",outPath=program+L"/winout60.dll";
  if(digest(bas.c_str())!=BAS_SHA||digest(dspPath.c_str())!="e1995ea477c7d87bd6ed75d7327376b9a5f9ed8d2564b0a42dbada9197b671cb"||digest(outPath.c_str())!="faa4f793b73e9e9e54a90982028bcca9d4b93c1453ebada63766e17f92ebfd2f")throw std::runtime_error("native plot build mismatch");
  SetDllDirectoryW(program.c_str());HMODULE library=LoadLibraryW(bas.c_str());if(!library)throw std::runtime_error("BAS60 initialization");
  HMODULE renderer=LoadLibraryW(outPath.c_str());HMODULE dsp=GetModuleHandleW(L"mapdsp60.dll");if(!renderer||!dsp)throw std::runtime_error("plot initialization");
  DWORD dep=0;BOOL permanent=FALSE;if(!GetProcessDEPPolicy(GetCurrentProcess(),&dep,&permanent)||dep!=0)throw std::runtime_error("legacy DEP compatibility unavailable");
  printf("{\"event\":\"host\",\"protocol\":1,\"pid\":%lu,\"bits\":32,\"dep_flags\":%lu,\"desktop\":\"%s\"}\n",GetCurrentProcessId(),dep,desktop_name().c_str());fflush(stdout);
  auto ctor=sym<Life>(library,"??0C60_WorkArea@@QAE@XZ");auto destroy=sym<Life>(library,"??1C60_WorkArea@@UAE@XZ");auto open=sym<Open>(library,"?_OpenArea@C60_WorkArea@@QAEFFFF@Z");auto load=sym<File>(library,"?_LoadAFile@C60_WorkArea@@QAEFPAD000@Z");
  using DrawArea=short(__stdcall*)(void*,short,short);Drawing drawing;begin_eps(drawing,renderer,dsp,argv[4],argv[5]);
  auto layersFile=ansi(argv[3]);std::ifstream layers(layersFile.data());int n=0;if(!(layers>>n)||n<1||n>100)throw std::runtime_error("layer count");
  for(int i=0;i<n;i++){
   int kind=-1,count=0;std::string file;if(!(layers>>kind>>file>>count)||kind<0||kind>2||count<0||count>1000000||file.find_first_of("/\\:")!=std::string::npos)throw std::runtime_error("layer parameters");std::vector<long> ids(count);for(long &id:ids)if(!(layers>>id)||id<1)throw std::runtime_error("layer physical IDs");
   Area area{};ctor(&area);require(open(&area,0,short(kind),0x4460),"plot work area");std::wstring relative(file.begin(),file.end()),layersPath(argv[3]);std::wstring full=layersPath.substr(0,layersPath.find_last_of(L"/\\"))+L"/"+relative;auto nativePath=ansi(full.c_str());short loaded=load(&area,nativePath.data(),nullptr,nullptr,nullptr);if(loaded<=0){fprintf(stderr,"layer_index=%d kind=%d input_exists=%s\n",i,kind,GetFileAttributesW(full.c_str())==INVALID_FILE_ATTRIBUTES?"false":"true");destroy(&area);throw std::runtime_error("plot file load");}
   short result=1;if(kind==2){using ReadRegion=short(__stdcall*)(int,long,long**,long*,void*);using DrawGeometry=short(__stdcall*)(void*,short,long*,long,void*,long);auto read=sym<ReadRegion>(library,MAKEINTRESOURCEA(44));auto draw=sym<DrawGeometry>(dsp,"?_DispReg@@YGFPAVKernelDriver@@FPAJJPAUREG_INFOstruct@@J@Z");for(long id:ids){long* topology=nullptr;long length=0;unsigned char info[128]{};result=read(area.area,id,&topology,&length,info);if(result>0)result=draw(drawing.dc,area.area,topology,length,info,0);if(result<=0)break;}}
   else {auto draw=sym<DrawArea>(dsp,kind==0?"_DispLinArea":"_DispPntArea");result=draw(drawing.dc,area.area,0);}
   destroy(&area);require(result,"native layer drawing");printf("{\"event\":\"layer_drawn\",\"index\":%d,\"kind\":%d,\"records\":%d}\n",i,kind,count);fflush(stdout);
  }
  drawing.destroy(drawing.dc);drawing.dc=nullptr;FreeLibrary(renderer);FreeLibrary(library);puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const std::exception&e){fprintf(stderr,"%s\n",e.what());return 1;}
}

void image_gcps(HMODULE image,void* handle){
 using Num=int(__cdecl*)(void*,unsigned*);using Get=int(__cdecl*)(void*,unsigned,double*,double*,double*,double*);
 unsigned count=0;int result=sym<Num>(image,"msGetGCPNumsOfMSI")(handle,&count);
 if(result==1000036)count=0;else if(result!=0||count>1000)throw std::runtime_error("MSI GCP count failed");
 printf("{\"event\":\"gcp_count\",\"count\":%u}\n",count);fflush(stdout);
 for(unsigned i=1;i<=count;++i){double x=0,y=0,X=0,Y=0;
  if(sym<Get>(image,"msGetOneGCPOfMSI")(handle,i,&x,&y,&X,&Y)!=0||!std::isfinite(x)||!std::isfinite(y)||!std::isfinite(X)||!std::isfinite(Y))throw std::runtime_error("MSI GCP readback");
  printf("{\"event\":\"gcp\",\"index\":%u,\"pixel_x\":%.17g,\"pixel_y\":%.17g,\"world_x\":%.17g,\"world_y\":%.17g}\n",i,x,y,X,Y);fflush(stdout);
 }
}

void assign_image_gcps(HMODULE image,char* filename,const wchar_t* command){
 auto name=ansi(command);std::ifstream stream(name.data());struct Gcp{double x,y,X,Y;};std::vector<Gcp> points;Gcp p{};
 while(stream>>p.x>>p.y>>p.X>>p.Y){if(points.size()>=1000||!std::isfinite(p.x)||!std::isfinite(p.y)||!std::isfinite(p.X)||!std::isfinite(p.Y))throw std::runtime_error("GCP arguments");points.push_back(p);}
 if(points.size()<3||!stream.eof())throw std::runtime_error("invalid GCP data");
 using OpenImage=int(__cdecl*)(char*,void**);using CloseImage=int(__cdecl*)(void*);using Allocate=int(__cdecl*)(void**,unsigned);
 using Set=int(__cdecl*)(void*,unsigned,double,double,double,double);using Assign=int(__cdecl*)(void*,void*);using Free=void(__cdecl*)(void*);
 void* handle=nullptr;void* controls=nullptr;
 if(sym<OpenImage>(image,"msOpenMsiFileForReadWrite")(filename,&handle)!=0||!handle)throw std::runtime_error("MSI write open");
 if(sym<Allocate>(image,"msAllocControlPointFile")(&controls,unsigned(points.size()))!=0||!controls)throw std::runtime_error("GCP allocate");
 using Initial=int(__cdecl*)(void*,unsigned);if(sym<Initial>(image,"msInitialControlPointFile")(controls,unsigned(points.size()))!=0)throw std::runtime_error("GCP initialise");
 for(unsigned i=0;i<points.size();++i){auto& q=points[i];if(sym<Set>(image,"msSetOneGCP")(controls,i+1,q.x,q.y,q.X,q.Y)!=0)throw std::runtime_error("GCP set");}
 int assigned=sym<Assign>(image,"msSetControlPointFileOfMSI")(handle,controls);sym<Free>(image,"msFreeControlPointFile")(controls);
 if(assigned!=0)throw std::runtime_error("GCP assignment");
 image_gcps(image,handle);
 int closed=sym<CloseImage>(image,"msCloseMsiFileForReadWrite")(handle);
 printf("{\"event\":\"gcp_saved\",\"native_result\":%d}\n",closed);fflush(stdout);
 if(closed!=0)throw std::runtime_error("MSI GCP save");
}

int image_main(int argc,wchar_t**argv){
 SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
 try{
  if(digest(argv[2])!=BAS_SHA||digest(argv[3])!="1a2098e4f0086d4f7fec7de2c6b1f7c3705b31ab2bf216ced9bb9a21647e7245")throw std::runtime_error("raster native profile mismatch");
  if(GetFileAttributesW(argv[6])!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("image output exists");
  std::wstring dll(argv[2]);SetDllDirectoryW(dll.substr(0,dll.find_last_of(L"/\\")).c_str());
  HMODULE bas=LoadLibraryExW(argv[2],nullptr,0);if(!bas)throw std::runtime_error("raster BAS initialization");
  HMODULE image=LoadLibraryExW(argv[3],nullptr,0);if(!image)throw std::runtime_error("raster MSI initialization");
  DWORD dep=0;BOOL permanent=FALSE;if(!GetProcessDEPPolicy(GetCurrentProcess(),&dep,&permanent)||dep!=0)throw std::runtime_error("legacy DEP compatibility unavailable");
  printf("{\"event\":\"host\",\"protocol\":1,\"pid\":%lu,\"bits\":32,\"dep_flags\":%lu,\"desktop\":\"%s\"}\n",GetCurrentProcessId(),dep,desktop_name().c_str());fflush(stdout);
  auto input=ansi(argv[5]),output=ansi(argv[6]);int result=-1;
  if(wcscmp(argv[4],L"to_msi")==0){
   using Convert=int(__cdecl*)(char*,char*);result=sym<Convert>(image,"msTifToMsi")(input.data(),output.data());
   if(result==0&&argc==8)assign_image_gcps(image,output.data(),argv[7]);
  }else if(wcscmp(argv[4],L"to_tiff")==0){
   using OpenImage=int(__cdecl*)(char*,void**);using CloseImage=int(__cdecl*)(void*);using Convert=int(__cdecl*)(void*,char*,unsigned);
   void* handle=nullptr;int opened=sym<OpenImage>(image,"msOpenMsiFileForRead")(input.data(),&handle);
   printf("{\"event\":\"image_opened\",\"native_result\":%d}\n",opened);fflush(stdout);
   if(opened!=0||!handle)throw std::runtime_error("MSI open failed");
   image_gcps(image,handle);
   result=sym<Convert>(image,"msMsiToTif")(handle,output.data(),0);
   int closed=sym<CloseImage>(image,"msCloseMsiFileForRead")(handle);
   printf("{\"event\":\"image_closed\",\"native_result\":%d}\n",closed);fflush(stdout);
   if(closed!=0)throw std::runtime_error("MSI close failed");
  }else throw std::runtime_error("unsupported image operation");
  printf("{\"event\":\"image_saved\",\"native_result\":%d}\n",result);fflush(stdout);
  if(result!=0||GetFileAttributesW(argv[6])==INVALID_FILE_ATTRIBUTES)throw std::runtime_error("native image conversion failed");
  // These legacy image modules are process-lifetime libraries. Explicit
  // FreeLibrary can block in their detach path after a writable RGB GCP file.
  // Normal process exit releases them after every MSI handle has been closed.
  puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const std::exception&e){fprintf(stderr,"%s\n",e.what());return 1;}
}

int project_main(int argc,wchar_t**argv){
 SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
 try{
  std::wstring program(argv[3]),bas=program+L"/bas60.dll",prj=program+L"/mapprj60.dll";
  if(digest(bas.c_str())!=BAS_SHA||digest(prj.c_str())!="faeeddbe9d4190f6b3645bfb0610a60bd68a6724f03d1e638932939f083f3291")throw std::runtime_error("project native profile mismatch");
  SetDllDirectoryW(program.c_str());HMODULE library=LoadLibraryW(bas.c_str());if(!library)throw std::runtime_error("project BAS initialization");
  HMODULE projectLib=LoadLibraryW(prj.c_str());if(!projectLib)throw std::runtime_error("project DLL initialization");
  DWORD dep=0;BOOL permanent=FALSE;if(!GetProcessDEPPolicy(GetCurrentProcess(),&dep,&permanent)||dep!=0)throw std::runtime_error("legacy DEP compatibility unavailable");
  printf("{\"event\":\"host\",\"protocol\":1,\"pid\":%lu,\"bits\":32,\"dep_flags\":%lu,\"desktop\":\"%s\"}\n",GetCurrentProcessId(),dep,desktop_name().c_str());fflush(stdout);
  using Create=void*(__stdcall*)(short);using Load=void*(__stdcall*)(char*,short,short);using Flag=short(__stdcall*)(void*,short);
  using Rename=void(__stdcall*)(void*,char*);using Append=short(__stdcall*)(void*,char*,char*,char);using Save=short(__stdcall*)(void*,char*);
  using Size=short(__stdcall*)(void*);using Item=short(__stdcall*)(void*,int);using ItemName=const char*(__stdcall*)(void*,int);
  void* project=nullptr;
  if(wcscmp(argv[2],L"create")==0){
   if(GetFileAttributesW(argv[5])!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("project output exists");
   project=sym<Create>(projectLib,"_PRJCreateProject")(0);if(!project)throw std::runtime_error("project create");
   sym<Flag>(projectLib,"_PRJSetDispMsgFlag")(project,0);auto output=ansi(argv[5]);sym<Rename>(projectLib,"_PRJSetName")(project,output.data());
   auto listName=ansi(argv[4]);std::ifstream list(listName.data());int count=0;if(!(list>>count)||count<1||count>100)throw std::runtime_error("project layer count");
   std::wstring listPath(argv[4]);auto root=listPath.substr(0,listPath.find_last_of(L"/\\"));
   if(!SetCurrentDirectoryW(root.c_str()))throw std::runtime_error("project layer directory");
   for(int i=0;i<count;++i){std::string name;if(!(list>>name)||name.find_first_of("/\\:")!=std::string::npos)throw std::runtime_error("project layer name");
    auto path=ansi((root+L"/"+std::wstring(name.begin(),name.end())).c_str());
    require(sym<Append>(projectLib,"_PRJAppend")(project,path.data(),nullptr,0),"project append");
    // PROJECTITEM's verified packed SDK layout has a 260-byte filename at +2.
    // Update only our new project's reference; false explicitly forbids the
    // SDK's optional physical-file rename operation.
    unsigned char item[400]{};using GetItem=short(__stdcall*)(void*,short,void*);using Update=short(__stdcall*)(void*,short,void*,short);
    require(sym<GetItem>(projectLib,"_PRJGetItem")(project,short(i),item),"project item readback");
    memset(item+2,0,260);if(name.size()>=260)throw std::runtime_error("project relative path");memcpy(item+2,name.c_str(),name.size());
    require(sym<Update>(projectLib,"_PRJUpdate")(project,short(i),item,0),"project portable reference");
   }
   require(sym<Save>(projectLib,"_PRJSaveAsProject")(project,output.data()),"project save");
  }else if(wcscmp(argv[2],L"inspect")==0||wcscmp(argv[2],L"plot")==0){
   // The DLL's OpenFile gate has the old OFSTRUCT path limit. Resolve the
   // owned invocation cwd and pass only the filename, never alter a system path.
   std::wstring full(argv[4]);auto split=full.find_last_of(L"/\\");
   if(split==std::wstring::npos||!SetCurrentDirectoryW(full.substr(0,split).c_str()))throw std::runtime_error("project input directory");
   auto input=ansi(full.substr(split+1).c_str());
   // PRJLoadAProject uses OpenFile/OFSTRUCT, which can reject a long absolute
   // path even when passed a basename. Use the exported class reader on our
   // own empty project instead, after checking the actual file exists.
   if(GetFileAttributesW(full.c_str())==INVALID_FILE_ATTRIBUTES)throw std::runtime_error("project input missing");
   project=sym<Create>(projectLib,"_PRJCreateProject")(0);if(!project)throw std::runtime_error("project allocation");
   auto fullName=ansi(full.c_str());sym<Rename>(projectLib,"_PRJSetName")(project,fullName.data());
   using Read=void(__thiscall*)(void*,char*);sym<Read>(projectLib,"?readproject@MapProject@@UAEXPAD@Z")(project,input.data());
   sym<Flag>(projectLib,"_PRJSetDispMsgFlag")(project,0);
  }else throw std::runtime_error("unsupported project operation");
  int count=sym<Size>(projectLib,"_PRJGetFileNum")(project);if(count<1||count>100)throw std::runtime_error("project loaded layer count");
  printf("{\"event\":\"project\",\"layers\":%d}\n",count);fflush(stdout);
  bool plotting=wcscmp(argv[2],L"plot")==0;Drawing drawing;HMODULE dsp=nullptr;
  if(plotting){
   if(argc!=7)throw std::runtime_error("project plot arguments");auto rendererPath=program+L"/winout60.dll",dspPath=program+L"/mapdsp60.dll";
   if(digest(rendererPath.c_str())!="faa4f793b73e9e9e54a90982028bcca9d4b93c1453ebada63766e17f92ebfd2f"||digest(dspPath.c_str())!="e1995ea477c7d87bd6ed75d7327376b9a5f9ed8d2564b0a42dbada9197b671cb")throw std::runtime_error("project renderer profile");
   HMODULE renderer=LoadLibraryW(rendererPath.c_str());dsp=GetModuleHandleW(L"mapdsp60.dll");if(!renderer||!dsp)throw std::runtime_error("project renderer initialization");begin_eps(drawing,renderer,dsp,argv[5],argv[6]);
  }
  std::wstring actualProject(wcscmp(argv[2],L"create")==0?argv[5]:argv[4]);auto actualRoot=actualProject.substr(0,actualProject.find_last_of(L"/\\"));
  for(int i=0;i<count;++i){
   short kind=sym<Item>(projectLib,"_PRJGetItemType")(project,i);if((kind<0||kind>2)&&kind!=11)throw std::runtime_error("project layer type");
   auto name=sym<ItemName>(projectLib,"_PRJGetItemName")(project,i);if(!name||strlen(name)>260)throw std::runtime_error("project layer path");
   std::string hex;for(unsigned char c:std::string(name)){char pair[3];sprintf_s(pair,"%02x",c);hex+=pair;}
   if(!SetCurrentDirectoryW(actualRoot.c_str()))throw std::runtime_error("project layer cwd");
   if(kind==11){
    using Find=void*(__thiscall*)(void*,short);using Bounds=short(__thiscall*)(void*,RectD*);
    void* item=sym<Find>(projectLib,"?FindItem@MapProject@@QAEPAVBaseItem@@F@Z")(project,short(i));RectD range{};
    if(!item||sym<Bounds>(projectLib,"?GetRect@msiImageItem@@UAEFPAUD_RECT@@@Z")(item,&range)<=0||!std::isfinite(range.xmin)||!std::isfinite(range.ymin)||!std::isfinite(range.xmax)||!std::isfinite(range.ymax)||range.xmin>=range.xmax||range.ymin>=range.ymax)throw std::runtime_error("project MSI native extent");
    printf("{\"event\":\"project_layer\",\"index\":%d,\"kind\":11,\"active\":1,\"native_bounds\":[%.17g,%.17g,%.17g,%.17g],\"path_gbk_hex\":\"%s\"}\n",i+1,range.xmin,range.ymin,range.xmax,range.ymax,hex.c_str());fflush(stdout);
    if(plotting){using Display=short(__thiscall*)(void*,void*);require(sym<Display>(projectLib,"?Display@msiImageItem@@UAEFPAVKernelDriver@@@Z")(item,drawing.dc),"project MSI drawing");printf("{\"event\":\"layer_drawn\",\"index\":%d,\"kind\":11,\"records\":1}\n",i);fflush(stdout);}continue;
   }
   using OpenItem=short(__thiscall*)(void*,short);require(sym<OpenItem>(projectLib,"?sOpenItemArea@MapProject@@QAEFF@Z")(project,short(i)),"project layer open");
   Area area{};area.area=sym<Item>(projectLib,"_PRJGetItemArea")(project,i);area.open=1;
   auto countFn=sym<Count>(library,kind==0?"?_GetLinNum@C60_WorkArea@@QAEFPAJ0@Z":kind==1?"?_GetPntNum@C60_WorkArea@@QAEFPAJ0@Z":"?_GetRegNum@C60_WorkArea@@QAEFPAJ0@Z");
   long active=-1,upper=-1;require(countFn(&area,&active,&upper),"project layer count readback");
   printf("{\"event\":\"project_layer\",\"index\":%d,\"kind\":%d,\"area\":%d,\"active\":%ld,\"path_gbk_hex\":\"%s\"}\n",i+1,kind,area.area,active,hex.c_str());fflush(stdout);
   if(plotting){
    if(kind==2){
     if(upper>1000000)throw std::runtime_error("project plot region bound");using Read=short(__stdcall*)(int,long,long**,long*,void*);using Draw=short(__stdcall*)(void*,short,long*,long,void*,long);
     auto get=sym<Read>(library,MAKEINTRESOURCEA(44));auto draw=sym<Draw>(dsp,"?_DispReg@@YGFPAVKernelDriver@@FPAJJPAUREG_INFOstruct@@J@Z");long drawn=0;
     for(long id=1;id<=upper;++id){long* topology=nullptr;long length=0;unsigned char info[128]{};if(get(area.area,id,&topology,&length,info)<=0)continue;require(draw(drawing.dc,area.area,topology,length,info,0),"project region drawing");++drawn;}if(drawn!=active)throw std::runtime_error("project region drawing count");
    }else{using Draw=short(__stdcall*)(void*,short,short);require(sym<Draw>(dsp,kind==0?"_DispLinArea":"_DispPntArea")(drawing.dc,area.area,0),"project vector drawing");}
    printf("{\"event\":\"layer_drawn\",\"index\":%d,\"kind\":%d,\"records\":%ld}\n",i,kind,active);fflush(stdout);
   }
  }
  if(plotting){drawing.destroy(drawing.dc);drawing.dc=nullptr;}
  sym<Flag>(projectLib,"_PRJSetModifyFlg")(project,0);using Delete=short(__stdcall*)(void*);
  short closed=sym<Delete>(projectLib,"_PRJDeleteProject")(project);if(closed==2)throw std::runtime_error("project close refused");
  puts("{\"event\":\"project_closed\"}");puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const std::exception&e){fprintf(stderr,"%s\n",e.what());return 1;}
}

int wmain(int argc,wchar_t**argv){
 try{bind_private_desktop();}catch(const std::exception&e){fprintf(stderr,"%s\n",e.what());return 1;}
 if((argc==7||argc==8)&&wcscmp(argv[1],L"image")==0)return image_main(argc,argv);
 if((argc==6||argc==7)&&wcscmp(argv[1],L"project")==0)return project_main(argc,argv);
 if(argc==6&&wcscmp(argv[1],L"plot")==0)return plot_main(argc,argv);
 if(argc==7&&wcscmp(argv[1],L"workspace")==0)return workspace_main(argv);
 // op, DLL, input, native kind (0 line/1 point/2 polygon), output or -, command file or -.
 if(argc!=7)return 2;SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
 try{
  std::wstring op(argv[1]);if(op!=L"inspect"&&op!=L"resave"&&op!=L"point.move"&&op!=L"feature.delete")throw std::runtime_error("unsupported operation");
  int kind=-1;if(wcscmp(argv[4],L"0")==0)kind=0;if(wcscmp(argv[4],L"1")==0)kind=1;if(wcscmp(argv[4],L"2")==0)kind=2;if(kind<0)throw std::runtime_error("native kind");
  if(op==L"point.move"&&kind!=1)throw std::runtime_error("point operation requires WT");
  if(digest(argv[2])!=BAS_SHA)throw std::runtime_error("BAS60 build mismatch");
  std::wstring dll(argv[2]);SetDllDirectoryW(dll.substr(0,dll.find_last_of(L"/\\")).c_str());
  Session s;s.library=LoadLibraryExW(argv[2],nullptr,0);if(!s.library)throw std::runtime_error("native initialization failed");
  DWORD dep=0;BOOL permanent=FALSE;if(!GetProcessDEPPolicy(GetCurrentProcess(),&dep,&permanent)||dep!=0)throw std::runtime_error("legacy DEP compatibility unavailable");
  printf("{\"event\":\"host\",\"protocol\":1,\"pid\":%lu,\"bits\":32,\"dep_flags\":%lu,\"desktop\":\"%s\"}\n",GetCurrentProcessId(),dep,desktop_name().c_str());fflush(stdout);
  auto ctor=sym<Life>(s.library,"??0C60_WorkArea@@QAE@XZ");s.destroy=sym<Life>(s.library,"??1C60_WorkArea@@UAE@XZ");auto open=sym<Open>(s.library,"?_OpenArea@C60_WorkArea@@QAEFFFF@Z");auto load=sym<File>(s.library,"?_LoadAFile@C60_WorkArea@@QAEFPAD000@Z");
  auto count=sym<Count>(s.library,kind==0?"?_GetLinNum@C60_WorkArea@@QAEFPAJ0@Z":kind==1?"?_GetPntNum@C60_WorkArea@@QAEFPAJ0@Z":"?_GetRegNum@C60_WorkArea@@QAEFPAJ0@Z");
  ctor(&s.area);s.constructed=true;require(open(&s.area,0,short(kind),0x4460),"open failed");auto in=ansi(argv[3]);require(load(&s.area,in.data(),nullptr,nullptr,nullptr),"read failed");counts(&s.area,count,"loaded");using Bounds=short(__stdcall*)(int,void*);auto boundsFn=sym<Bounds>(s.library,MAKEINTRESOURCEA(174));double bounds[4]{};short boundsStatus=boundsFn(s.area.area,bounds);if(boundsStatus>0&&std::isfinite(bounds[0])&&std::isfinite(bounds[1])&&std::isfinite(bounds[2])&&std::isfinite(bounds[3]))printf("{\"event\":\"bounds\",\"native_bounds\":[%.17g,%.17g,%.17g,%.17g]}\n",bounds[0],bounds[1],bounds[2],bounds[3]);fflush(stdout);
  if(op==L"point.move"){
   auto command=ansi(argv[6]);std::ifstream stream(command.data());if(!stream)throw std::runtime_error("command file");
   auto get=sym<RawPos>(s.library,MAKEINTRESOURCEA(1558));auto set=sym<RawPos>(s.library,MAKEINTRESOURCEA(1561));long id;double xy[2];int n=0;
   while(stream>>id>>xy[0]>>xy[1]){if(++n>1000||id<1||!std::isfinite(xy[0])||!std::isfinite(xy[1]))throw std::runtime_error("point arguments");double before[2];require(get(s.area.area,id,before,2),"active point required");require(set(s.area.area,id,xy,2),"point update failed");double after[2];require(get(s.area.area,id,after,2),"point readback failed");if(after[0]!=xy[0]||after[1]!=xy[1])throw std::runtime_error("point readback differs");printf("{\"event\":\"point_moved\",\"record_id\":%ld,\"x\":%.17g,\"y\":%.17g}\n",id,after[0],after[1]);fflush(stdout);}
   if(!n||!stream.eof())throw std::runtime_error("invalid command data");
  }
  if(op==L"feature.delete"){
   using Del=short(__thiscall*)(Area*,long);auto del=sym<Del>(s.library,kind==2?"?_DelReg@C60_WorkArea@@QAEFJ@Z":kind==1?"?_DelPnt@C60_WorkArea@@QAEFJ@Z":"?_DelLin@C60_WorkArea@@QAEFJ@Z");auto command=ansi(argv[6]);std::ifstream stream(command.data());long id;int n=0;
   while(stream>>id){if(++n>1000||id<1)throw std::runtime_error("delete IDs");require(del(&s.area,id),"native deletion failed");printf("{\"event\":\"feature_deleted\",\"record_id\":%ld}\n",id);fflush(stdout);}if(!n||!stream.eof())throw std::runtime_error("delete command data");
  }
  if(op!=L"inspect"){
   if(GetFileAttributesW(argv[5])!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("output exists");
   auto out=ansi(argv[5]);auto save=sym<File>(s.library,"?_SaveAFile@C60_WorkArea@@QAEFPAD000@Z");require(save(&s.area,out.data(),nullptr,nullptr,nullptr),"save failed");counts(&s.area,count,"saved");puts("{\"event\":\"saved\"}");fflush(stdout);
  }
  puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const std::exception&e){fprintf(stderr,"%s\n",e.what());return 1;}
}
