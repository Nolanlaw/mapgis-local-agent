// Exact-build BAS60 worker. Own process only; never attaches to an editor.
// C60_WorkArea has pure virtual coordinate converters. Geometry uses the verified
// ordinal API instead. Every operation is staged and independently checked by Python.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
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
#include <cstdint>
#include <algorithm>
#include "PrivateDesktop.h"
#pragma comment(lib,"advapi32.lib")
#pragma comment(lib,"user32.lib")
static const char* BAS_SHA="680029d3ed7a04338a6799f1e16cab30d6e421d13e3d8c66bbeccb057a50ee24";
LONG WINAPI native_failure(EXCEPTION_POINTERS* exception){
 HMODULE module=nullptr;char path[MAX_PATH]{};auto address=exception->ExceptionRecord->ExceptionAddress;
 if(GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,reinterpret_cast<LPCSTR>(address),&module))GetModuleFileNameA(module,path,MAX_PATH);
 const char* name=strrchr(path,'\\');name=name?name+1:path;
 fprintf(stderr,"native exception code=0x%08lx module=%s offset=0x%lx\n",exception->ExceptionRecord->ExceptionCode,name,static_cast<unsigned long>(reinterpret_cast<uintptr_t>(address)-reinterpret_cast<uintptr_t>(module)));fflush(stderr);
 return EXCEPTION_CONTINUE_SEARCH;
}
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
using PointInfo=short(__stdcall*)(int,long,void*);
void normalize_empty_attributes(HMODULE library,Area& area){
 // This pinned loader allocates a zero-element CFIELD_HEAD array, but its
 // destructor dispatches through element zero. Canonical empty constructors
 // use nullptr. Release only the verified zero cookie with its own allocator.
 using Context=void*(__stdcall*)(int);auto context=static_cast<unsigned char*>(sym<Context>(library,MAKEINTRESOURCEA(93))(area.area));
 if(!context)return;HMODULE root=GetModuleHandleW(L"basroot.dll");if(!root)throw std::runtime_error("attribute module unavailable");
 auto plain=GetProcAddress(root,"??_7CATT_STRU@@6B@");auto internal=GetProcAddress(root,"??_7C_in_ATT_STRU@@6B@");
 bool verified=false;std::set<void*> seen;
 for(int offset=3679;offset<=3699;offset+=4){
  unsigned char* table=nullptr;memcpy(&table,context+offset,4);if(!table||!seen.insert(table).second)continue;
  void* vtable=nullptr;short count=-1;void* fields=nullptr;memcpy(&vtable,table,4);memcpy(&count,table+8,2);memcpy(&fields,table+18,4);
  if(count||!fields)continue;
  if(!plain||!internal||(vtable!=plain&&vtable!=internal))throw std::runtime_error("unknown empty attribute object");
  unsigned long cookie=1;auto allocation=static_cast<unsigned char*>(fields)-4;memcpy(&cookie,allocation,4);
  if(cookie)throw std::runtime_error("unknown empty attribute array");
  if(!verified){wchar_t path[MAX_PATH]{};if(!GetModuleFileNameW(root,path,MAX_PATH)||digest(path)!="d1109d09d3708911a68a490fc86d64e8fbf30b3f53a7ffce8de2241ec5cc36ba")throw std::runtime_error("empty attribute build mismatch");verified=true;}
  using Delete=void(__cdecl*)(void*);auto release=sym<Delete>(GetModuleHandleW(L"MFC42.DLL"),MAKEINTRESOURCEA(825));
  fields=nullptr;memcpy(table+18,&fields,4);release(allocation);
 }
}
struct Session {HMODULE library=nullptr;Area area{};Life destroy=nullptr;bool constructed=false;
 void close(){if(constructed){normalize_empty_attributes(library,area);destroy(&area);constructed=false;}}
 // BAS60 owns background threads. Keep its module loaded until normal process
 // exit, as for the MSI host; FreeLibrary can block after a completed read.
 ~Session(){close();}
};
struct PreserveAttributes {
 using Get=short(__thiscall*)(Area*);using Set=short(__thiscall*)(Area*,char);
 Area* area;Set set;char previous;
 PreserveAttributes(Session& session):area(&session.area),set(sym<Set>(session.library,"?_SetAutoCalDefAttFlag@C60_WorkArea@@QAEFD@Z")),previous(0){
  auto get=sym<Get>(session.library,"?_GetAutoCalDefAttFlag@C60_WorkArea@@QAEFXZ");previous=char(get(area));set(area,0);
  if(get(area)!=0)throw std::runtime_error("disable automatic default attributes failed");
 }
 ~PreserveAttributes(){set(area,previous);}
};
struct NativeAttributeDataOrigin {
 unsigned char* table=nullptr;long previous=0;
 NativeAttributeDataOrigin(Session& s,int kind){
  // Pinned DelLin/DelReg -> sub_10013440 seeks table+22 + id*width.
  // The loaded custom schema can retain a record origin from the default
  // field layout. Check the actual serialized header before correcting it.
  // A temporary origin correction prevents
  // a deletion flag from overwriting an unrelated record's field payload.
  using Context=void*(__stdcall*)(int);auto context=static_cast<unsigned char*>(sym<Context>(s.library,MAKEINTRESOURCEA(93))(s.area.area));
  if(!context)throw std::runtime_error("attribute deletion context");
  if(context[157]&0x10)return; // Original delete skips its attribute branch.
  long database=0;memcpy(&database,context+323,4);if(database)throw std::runtime_error("external attribute database deletion profile");
  const int offsets[]={3683,3679,3687},streams[]={2024,2024,2052};memcpy(&table,context+offsets[kind],4);
  if(!table)throw std::runtime_error("attribute deletion structure");
  HMODULE root=GetModuleHandleW(L"basroot.dll");void* vtable=nullptr;memcpy(&vtable,table,4);
  if(!root||(vtable!=GetProcAddress(root,"??_7CATT_STRU@@6B@")&&vtable!=GetProcAddress(root,"??_7C_in_ATT_STRU@@6B@")))throw std::runtime_error("attribute deletion structure profile");
  wchar_t path[MAX_PATH]{};if(!GetModuleFileNameW(root,path,MAX_PATH)||digest(path)!="d1109d09d3708911a68a490fc86d64e8fbf30b3f53a7ffce8de2241ec5cc36ba")throw std::runtime_error("attribute deletion build profile");
  short fields=0;long width=0;memcpy(&fields,table+8,2);memcpy(&width,table+14,4);memcpy(&previous,table+22,4);
  if(fields<0||width<1||width>32767)throw std::runtime_error("attribute deletion layout");
  long origin=348+long(fields)*39;if(previous==origin)return;
  void* stream=nullptr;memcpy(&stream,context+streams[kind],4);if(!stream)throw std::runtime_error("attribute deletion stream");
  auto methods=*static_cast<void***>(stream);using Seek=long(__thiscall*)(void*,long,unsigned int);using Read=unsigned int(__thiscall*)(void*,void*,unsigned int);
  auto seek=reinterpret_cast<Seek>(methods[2]);auto read=reinterpret_cast<Read>(methods[5]);long position=seek(stream,0,1);if(position<0)throw std::runtime_error("attribute deletion stream position");
  seek(stream,0,0);unsigned char header[348]{};unsigned int bytes=read(stream,header,sizeof(header));seek(stream,position,0);
  short savedFields=0,savedWidth=0;memcpy(&savedFields,header+322,2);memcpy(&savedWidth,header+328,2);
  if(bytes!=sizeof(header)||header[0]!=0x60||header[1]!=0x44||header[320]!=0x60||header[321]!=0x44||savedFields!=fields||savedWidth!=width)throw std::runtime_error("attribute deletion serialized header not verified");
  if(previous<348||(previous-origin)%39)throw std::runtime_error("attribute deletion data origin not verified");
  seek(stream,origin,0);unsigned char flag=255;bytes=read(stream,&flag,1);seek(stream,position,0);
  if(bytes!=1||flag>1)throw std::runtime_error("attribute deletion reserved record not verified");
  memcpy(table+22,&origin,4);
  printf("{\"event\":\"attribute_delete_origin_normalized\",\"kind\":%d,\"fields\":%d,\"row_width\":%ld,\"previous_offset\":%ld}\n",kind,int(fields),width,previous);fflush(stdout);
 }
 ~NativeAttributeDataOrigin(){if(table)memcpy(table+22,&previous,4);}
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
 }else if(op==L"point.text"){
  if(kind!=1)throw std::runtime_error("annotation text requires WT");
  // Original named Get/UpdatePnt wrappers establish these seven arguments.
  // Both use dimension3 here to preserve every stored XYZ ordinate unchanged.
  using GetNote=short(__stdcall*)(int,long,void*,char**,short*,void*,int);
  using SetNote=short(__stdcall*)(int,long,void*,char*,short,void*,int);
  auto get=sym<GetNote>(s.library,MAKEINTRESOURCEA(1557));auto set=sym<SetNote>(s.library,MAKEINTRESOURCEA(1560));
  struct Note {long id;double xyz[3];std::vector<char> data;std::vector<unsigned char> info;};std::vector<Note> values;
  long id=0;std::string encoded;size_t total=0;
  while(stream>>id>>encoded){
   if(values.size()>=1000||id<1||!unique.insert(id).second||encoded.empty()||encoded.size()%2||encoded.size()>65534||encoded.find_first_not_of("0123456789abcdef")!=std::string::npos)throw std::runtime_error("annotation arguments");
   Note value{};value.id=id;value.info.resize(94);value.data.reserve(encoded.size()/2+1);
   for(size_t i=0;i<encoded.size();i+=2){char byte=char(strtoul(encoded.substr(i,2).c_str(),nullptr,16));if(!byte)throw std::runtime_error("annotation embedded NUL");value.data.push_back(byte);}
   total+=value.data.size();if(total>1048576)throw std::runtime_error("annotation batch size");value.data.push_back(0);
   require(get(s.area.area,id,value.xyz,nullptr,nullptr,value.info.data(),3),"active annotation required");
   if(value.info[0]!=0)throw std::runtime_error("annotation kind0 required");for(double v:value.xyz)if(!std::isfinite(v))throw std::runtime_error("annotation source XYZ");
   values.push_back(std::move(value));
  }
  if(values.empty()||!stream.eof())throw std::runtime_error("invalid annotation data");PreserveAttributes preserve(s);
  for(auto& value:values){
   // nullptr info retains the existing style in the original API. Passing a
   // new info block would also normalize unrelated native layer flags.
   short length=short(value.data.size()-1);require(set(s.area.area,value.id,value.xyz,value.data.data(),length,nullptr,3),"annotation update failed");
   double xyz[3]{};char* data=nullptr;short actual=0;std::vector<unsigned char> info(94);
   require(get(s.area.area,value.id,xyz,&data,&actual,info.data(),3),"annotation readback failed");
   if(actual!=length||!data||memcmp(data,value.data.data(),length)||memcmp(xyz,value.xyz,sizeof(xyz))||memcmp(info.data(),value.info.data(),62))throw std::runtime_error("annotation readback differs");
   double bounds[4]{};memcpy(bounds,info.data()+62,32);
   printf("{\"event\":\"annotation_updated\",\"record_id\":%ld,\"gbk_bytes\":%d,\"native_bounds\":[%.17g,%.17g,%.17g,%.17g]}\n",value.id,int(length),bounds[0],bounds[1],bounds[2],bounds[3]);fflush(stdout);
  }
 }else if(op==L"point.style"){
  if(kind!=1)throw std::runtime_error("point style requires WT");
  // PNT_INFO ordinary note/symbol union, from installed SDK type metadata,
  // normal converters and original CLN->WT output. No arbitrary byte writes.
  const int offsets[2][11]={{44,52,14,2,6,10,18,20,22,-1,-1},{44,52,14,6,10,-1,-1,-1,-1,2,18}};
  struct Style {long id;std::vector<unsigned char> info;};std::vector<Style> values;long id=0;int count=0;
  auto get=sym<PointInfo>(s.library,MAKEINTRESOURCEA(69));auto set=sym<PointInfo>(s.library,MAKEINTRESOURCEA(70));
  while(stream>>id>>count){
   if(values.size()>=1000||id<1||!unique.insert(id).second||count<1||count>11)throw std::runtime_error("point style arguments");
   Style value{id,std::vector<unsigned char>(94)};require(get(s.area.area,id,value.info.data()),"active point style required");
   int type=value.info[0];if(type!=0&&type!=1)throw std::runtime_error("ordinary note/symbol required");std::set<int> fields;
   for(int i=0;i<count;++i){int field=-1;double number=0;
    if(!(stream>>field>>number)||field<0||field>10||offsets[type][field]<0||!fields.insert(field).second||!std::isfinite(number))throw std::runtime_error("point style field");
    unsigned char* target=value.info.data()+offsets[type][field];
    if(field==0||field==1||(field>=6&&field<=9)){
     if(number!=std::floor(number)||number<0||number>(field==9?32767:255))throw std::runtime_error("point style integer");
     if(field==0){long integer=long(number);memcpy(target,&integer,4);}else{short integer=short(number);memcpy(target,&integer,2);}
    }else{
     if(std::abs(number)>3.4028234663852886e38||(field!=2&&number<0))throw std::runtime_error("point style float");
     float numeric=float(number);if((field==3||field==4)&&numeric<=0)throw std::runtime_error("point style size");memcpy(target,&numeric,4);
    }
   }
   values.push_back(std::move(value));
  }
  if(values.empty()||!stream.eof())throw std::runtime_error("invalid point style data");
  for(auto& value:values){
   require(set(s.area.area,value.id,value.info.data()),"point style update failed");std::vector<unsigned char> checked(94);
   require(get(s.area.area,value.id,checked.data()),"point style readback failed");
   if(memcmp(checked.data(),value.info.data(),62)!=0)throw std::runtime_error("point style readback differs");
   double bounds[4]{};memcpy(bounds,checked.data()+62,32);for(double v:bounds)if(!std::isfinite(v))throw std::runtime_error("point style bounds");
   printf("{\"event\":\"point_style_updated\",\"record_id\":%ld,\"native_bounds\":[%.17g,%.17g,%.17g,%.17g]}\n",value.id,bounds[0],bounds[1],bounds[2],bounds[3]);fflush(stdout);
  }
 }else if(op==L"point.radius"){
  if(kind!=1)throw std::runtime_error("radius operation requires WT");
  struct Radius {long id;double value;std::vector<unsigned char> info;};std::vector<Radius> values;long id=0;double radius=0;
  auto get=sym<PointInfo>(s.library,MAKEINTRESOURCEA(69));auto set=sym<PointInfo>(s.library,MAKEINTRESOURCEA(70));
  while(stream>>id>>radius){
   if(values.size()>=1000||id<1||!unique.insert(id).second||!std::isfinite(radius)||radius<=0)throw std::runtime_error("radius arguments");
   Radius value{id,radius,std::vector<unsigned char>(94)};require(get(s.area.area,id,value.info.data()),"active primitive required");
   if(value.info[0]!=2&&value.info[0]!=3)throw std::runtime_error("circle/arc primitive required");values.push_back(std::move(value));
  }
  if(values.empty()||!stream.eof())throw std::runtime_error("invalid radius data");
  for(auto& value:values){
   memcpy(value.info.data()+2,&value.value,sizeof(double));require(set(s.area.area,value.id,value.info.data()),"radius update failed");
   std::vector<unsigned char> checked(94);require(get(s.area.area,value.id,checked.data()),"radius readback failed");double actual=0;memcpy(&actual,checked.data()+2,sizeof(double));
   if(actual!=value.value||checked[0]!=value.info[0])throw std::runtime_error("radius readback differs");
  }
 }else if(op==L"line.replace"||op==L"arc.replace"||op==L"node.move"){
  if((op==L"line.replace"&&kind!=0)||((op==L"arc.replace"||op==L"node.move")&&kind!=2))throw std::runtime_error("line/arc native kind");
  struct Line {long id;std::vector<double> xy;};std::vector<Line> lines;long id=0,n=0;size_t vertices=0;
  while(stream>>id){
   if(!(stream>>n)||n<2||n>100000||lines.size()>=1000||id<1||!unique.insert(id).second||vertices+size_t(n)>1000000)throw std::runtime_error("line arguments");
   Line line{id,std::vector<double>(size_t(n)*2)};
   for(double& value:line.xy)if(!(stream>>value)||!std::isfinite(value))throw std::runtime_error("line vertices");
   vertices+=size_t(n);lines.push_back(std::move(line));
  }
  if(lines.empty()||!stream.eof())throw std::runtime_error("invalid line data");
  // BAS60 1551/1553: __stdcall, double XY arrays, dimension 2. The native
  // writer retains existing line parameters when the info pointer is null.
  using GetLine=short(__stdcall*)(int,long,void**,long*,void*,short*);
  using SetLine=short(__stdcall*)(int,long,void*,long,void*,int);
  // Only requested geometry changes; the legacy default-attribute calculator
  // otherwise rewrites user tables through its separate attribute API.
  PreserveAttributes preserve(s);
  auto get=sym<GetLine>(s.library,MAKEINTRESOURCEA(1551));auto set=sym<SetLine>(s.library,MAKEINTRESOURCEA(1553));
  for(auto& line:lines){void* xy=nullptr;long count=0;short dimension=2;require(get(s.area.area,line.id,&xy,&count,nullptr,&dimension),"active line required");}
  for(auto& line:lines){
   require(set(s.area.area,line.id,line.xy.data(),long(line.xy.size()/2),nullptr,2),"line update failed");
   void* data=nullptr;long count=0;short dimension=2;require(get(s.area.area,line.id,&data,&count,nullptr,&dimension),"line readback failed");
   if(!data||count!=long(line.xy.size()/2))throw std::runtime_error("line readback size differs");
   auto xy=static_cast<double*>(data);for(size_t i=0;i<line.xy.size();++i)if(xy[i]!=line.xy[i])throw std::runtime_error("line readback differs");
  }
 }else if(op==L"feature.delete"){
  long id;while(stream>>id){if(ids.size()>=1000||id<1||!unique.insert(id).second)throw std::runtime_error("delete IDs");ids.push_back(id);}if(ids.empty()||!stream.eof())throw std::runtime_error("invalid delete data");
  PreserveAttributes preserve(s);
  NativeAttributeDataOrigin origin(s,kind);
  using Del=short(__thiscall*)(Area*,long);auto del=sym<Del>(s.library,kind==2?"?_DelReg@C60_WorkArea@@QAEFJ@Z":kind==1?"?_DelPnt@C60_WorkArea@@QAEFJ@Z":"?_DelLin@C60_WorkArea@@QAEFJ@Z");for(long id:ids)require(del(&s.area,id),"native deletion failed");
 }else throw std::runtime_error("unsupported edit");
}

void load_area(Session& s,int kind,const std::wstring& path){
 s.close();s.area={};
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
struct Drawing {void* dc=nullptr;double scale=0,originX=0,originY=0;DrawingDrop destroy=nullptr;~Drawing(){if(dc)destroy(dc);}};
// Exact-build PROJECTHEAD fields are part of the private project object, not
// an editor's address space. Native saveproject writes this header unchanged;
// vDisplay composes these five parameters with the caller's drawing context.
struct ProjectEnvironment {double x,y,sx,sy,angle,width,height;};
ProjectEnvironment project_environment(void* project){
 ProjectEnvironment value{};memcpy(&value,static_cast<char*>(project)+2508,sizeof value);
 if(!std::isfinite(value.x)||!std::isfinite(value.y)||!std::isfinite(value.sx)||!std::isfinite(value.sy)||!std::isfinite(value.angle)||!std::isfinite(value.width)||!std::isfinite(value.height)||value.sx<0.001||value.sy<0.001||value.width<=0||value.height<=0)throw std::runtime_error("project environment");
 return value;
}
void project_environment_event(const ProjectEnvironment& e){
 printf("{\"event\":\"project_environment\",\"translation\":[%.17g,%.17g],\"scale\":[%.17g,%.17g],\"rotation_degrees\":%.17g,\"paper_mm\":[%.17g,%.17g]}\n",e.x,e.y,e.sx,e.sy,e.angle,e.width,e.height);fflush(stdout);
}
void compose_project_environment(Drawing& drawing,HMODULE dsp,const ProjectEnvironment& e){
 // Mirrors mapprj60 vDisplay(a3=1); the underlying vector records stay intact.
 double radians=-e.angle*0.017453292519943295,c=cos(radians),s=sin(radians);
 double ox=(c*drawing.originX-s*drawing.originY)/e.sx-e.x;
 double oy=(s*drawing.originX+c*drawing.originY)/e.sy-e.y;
 using Transform=short(__stdcall*)(void*,double,double,double,double,double);
 require(sym<Transform>(dsp,"_SetDispParam")(drawing.dc,ox,oy,drawing.scale*e.sx,drawing.scale*e.sy,e.angle),"project environment drawing");
 drawing.scale*=e.sx;
}
void bind_system_library(HMODULE library,HMODULE dsp,const wchar_t* folder){
 using Environment=void*(__stdcall*)(short);using Current=void*(__cdecl*)();using Change=short(__thiscall*)(void*,void*);
 void* original=sym<Environment>(library,MAKEINTRESOURCEA(226))(0);void* current=sym<Current>(dsp,"_GetCurrentLib")();
 if(!original||!current)throw std::runtime_error("native system library handles");
 // Exact installed profile: GISENV is 640 bytes, with the 128-byte system
 // library folder at +512. bas60_226 returns a disposable copy; colours
 // query a new copy each time. rt_LoadEnv returns the actual owned process
 // environment. Restore the full prior 640-byte record immediately, with
 // only our frozen snapshot folder changed. It never writes an env file.
 unsigned char environment[640];memcpy(environment,original,sizeof environment);auto path=ansi(folder);
 if(strlen(path.data())>=128)throw std::runtime_error("native system library path");
 memset(environment+512,0,128);memcpy(environment+512,path.data(),strlen(path.data()));
 HMODULE root=GetModuleHandleW(L"basroot.dll");wchar_t rootPath[MAX_PATH]{};
 if(!root||!GetModuleFileNameW(root,rootPath,MAX_PATH)||digest(rootPath)!="d1109d09d3708911a68a490fc86d64e8fbf30b3f53a7ffce8de2241ec5cc36ba")throw std::runtime_error("native environment build mismatch");
 using LoadEnvironment=void*(__stdcall*)(HMODULE);
 void* ownedEnvironment=sym<LoadEnvironment>(root,"rt_LoadEnv")(GetModuleHandleW(nullptr));
 if(!ownedEnvironment)throw std::runtime_error("native owned environment");
 memcpy(ownedEnvironment,environment,sizeof environment);
 void* fresh=sym<Environment>(library,MAKEINTRESOURCEA(226))(0);
 if(!fresh||memcmp(static_cast<unsigned char*>(fresh)+512,environment+512,128))throw std::runtime_error("native environment readback");
 require(sym<Change>(dsp,"?_ChangeMAPGISLib@MAPGISLib@@QAEFPAUGISENV@@@Z")(current,environment),"native system library binding");
 puts("{\"event\":\"system_library_bound\",\"scope\":\"isolated_drawing_process\"}");fflush(stdout);
}
void begin_eps(Drawing& drawing,HMODULE renderer,HMODULE dsp,const wchar_t* viewPath,const wchar_t* outputPath,bool postscript=false){
 if(GetFileAttributesW(outputPath)!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("plot output exists");
 auto viewFile=ansi(viewPath);std::ifstream view(viewFile.data());RectD box{};double width=0,height=0;
 if(!(view>>box.xmin>>box.ymin>>box.xmax>>box.ymax>>width>>height)||!std::isfinite(box.xmin)||!std::isfinite(box.ymin)||!std::isfinite(box.xmax)||!std::isfinite(box.ymax)||!(box.xmax>box.xmin)||!(box.ymax>box.ymin)||!(width>=1&&width<=2000&&height>=1&&height<=2000))throw std::runtime_error("plot view");
 using Make=void*(__stdcall*)(char*);using SetRange=void(__thiscall*)(void*,RectD);using Clip=short(__thiscall*)(void*,void*);using Transform=short(__stdcall*)(void*,double,double,double,double,double);
 auto make=sym<Make>(renderer,postscript?"?_GetCurvePSDC@@YGPAVKernelDriver@@PAD@Z":"?_GetEPSDC@@YGPAVKernelDriver@@PAD@Z");drawing.destroy=sym<DrawingDrop>(dsp,"_DeleteMyDC");auto out=ansi(outputPath);drawing.dc=make(out.data());if(!drawing.dc)throw std::runtime_error("create native drawing context");
 auto range=sym<SetRange>(renderer,postscript?"?set_range@HDCurvePostScriptDriver@@UAEXUD_RECT@@@Z":"?set_range@HDEPSDriver@@UAEXUD_RECT@@@Z");auto clip=sym<Clip>(dsp,"?_SetDispRect@KernelDriver@@QAEFPAUD_RECT@@@Z");auto transform=sym<Transform>(dsp,"_SetDispParam");range(drawing.dc,RectD{0,0,width,height});require(clip(drawing.dc,&box),"clip");double sx=width/(box.xmax-box.xmin),sy=height/(box.ymax-box.ymin),scale=sx<sy?sx:sy;double originX=box.xmin-(width-(box.xmax-box.xmin)*scale)/(2*scale),originY=box.ymin-(height-(box.ymax-box.ymin)*scale)/(2*scale);require(transform(drawing.dc,originX,originY,scale,scale,0),"plot transform");drawing.scale=scale;
 drawing.originX=originX;drawing.originY=originY;
 printf("{\"event\":\"plot_view\",\"native_display_scale\":%.17g}\n",scale);fflush(stdout);
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
  if(argc==7)bind_system_library(library,dsp,argv[6]);
  using DrawArea=short(__stdcall*)(void*,short,short);Drawing drawing;begin_eps(drawing,renderer,dsp,argv[4],argv[5],wcscmp(argv[1],L"plot.ps")==0);
  auto layersFile=ansi(argv[3]);std::ifstream layers(layersFile.data());int n=0;if(!(layers>>n)||n<1||n>100)throw std::runtime_error("layer count");
  for(int i=0;i<n;i++){
   int kind=-1,count=0;std::string file;if(!(layers>>kind>>file>>count)||kind<0||kind>2||count<0||count>1000000||file.find_first_of("/\\:")!=std::string::npos)throw std::runtime_error("layer parameters");std::vector<long> ids(count);for(long &id:ids)if(!(layers>>id)||id<1)throw std::runtime_error("layer physical IDs");
   Area area{};ctor(&area);require(open(&area,0,short(kind),0x4460),"plot work area");std::wstring relative(file.begin(),file.end()),layersPath(argv[3]);std::wstring full=layersPath.substr(0,layersPath.find_last_of(L"/\\"))+L"/"+relative;auto nativePath=ansi(full.c_str());short loaded=load(&area,nativePath.data(),nullptr,nullptr,nullptr);if(loaded<=0){fprintf(stderr,"layer_index=%d kind=%d input_exists=%s\n",i,kind,GetFileAttributesW(full.c_str())==INVALID_FILE_ATTRIBUTES?"false":"true");destroy(&area);throw std::runtime_error("plot file load");}
   short result=1;if(kind==2){using ReadRegion=short(__stdcall*)(int,long,long**,long*,void*);using DrawGeometry=short(__stdcall*)(void*,short,long*,long,void*,long);auto read=sym<ReadRegion>(library,MAKEINTRESOURCEA(44));auto draw=sym<DrawGeometry>(dsp,"?_DispReg@@YGFPAVKernelDriver@@FPAJJPAUREG_INFOstruct@@J@Z");for(long id:ids){long* topology=nullptr;long length=0;unsigned char info[128]{};result=read(area.area,id,&topology,&length,info);if(result>0)result=draw(drawing.dc,area.area,topology,length,info,0);if(result<=0)break;}}
   else {auto draw=sym<DrawArea>(dsp,kind==0?"_DispLinArea":"_DispPntArea");result=draw(drawing.dc,area.area,0);}
   normalize_empty_attributes(library,area);destroy(&area);require(result,"native layer drawing");printf("{\"event\":\"layer_drawn\",\"index\":%d,\"kind\":%d,\"records\":%d}\n",i,kind,count);fflush(stdout);
  }
  drawing.destroy(drawing.dc);drawing.dc=nullptr;printf("{\"event\":\"plot_saved\",\"sha256\":\"%s\"}\n",digest(argv[5]).c_str());puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const std::exception&e){fprintf(stderr,"%s\n",e.what());return 1;}
}

void image_gcps(HMODULE image,void* handle){
 using Num=int(__cdecl*)(void*,unsigned*);using Get=int(__cdecl*)(void*,unsigned,double*,double*,double*,double*);
 unsigned count=0;int result=sym<Num>(image,"msGetGCPNumsOfMSI")(handle,&count);
 if(result==1000036)count=0;else if(result!=0||count>1000)throw std::runtime_error("MSI GCP count failed");
 printf("{\"event\":\"gcp_count\",\"count\":%u}\n",count);fflush(stdout);
 for(unsigned i=1;i<=count;++i){double x=0,y=0,X=0,Y=0;
  if(sym<Get>(image,"msGetOneGCPOfMSI")(handle,i,&x,&y,&X,&Y)!=0||!std::isfinite(x)||!std::isfinite(y)||!std::isfinite(X)||!std::isfinite(Y))throw std::runtime_error("MSI GCP readback");
  // The exact native GCP ABI stores target/map coordinates first, image
  // coordinates second. Keep the external protocol pixel -> world.
  printf("{\"event\":\"gcp\",\"index\":%u,\"pixel_x\":%.17g,\"pixel_y\":%.17g,\"world_x\":%.17g,\"world_y\":%.17g}\n",i,X,Y,x,y);fflush(stdout);
 }
}

void assign_image_gcps(HMODULE image,char* filename,const wchar_t* command,bool nativeCoordinates=false){
 auto name=ansi(command);std::ifstream stream(name.data());struct Gcp{double x,y,X,Y;};std::vector<Gcp> points;Gcp p{};
 while(stream>>p.x>>p.y>>p.X>>p.Y){if(points.size()>=1000||!std::isfinite(p.x)||!std::isfinite(p.y)||!std::isfinite(p.X)||!std::isfinite(p.Y))throw std::runtime_error("GCP arguments");points.push_back(p);}
 if(points.size()<3||!stream.eof())throw std::runtime_error("invalid GCP data");
 using OpenImage=int(__cdecl*)(char*,void**);using CloseImage=int(__cdecl*)(void*);using Allocate=int(__cdecl*)(void*,unsigned);
 using Set=int(__cdecl*)(void*,unsigned,double,double,double,double);
 void* handle=nullptr;void* controls=nullptr;
 if(sym<OpenImage>(image,"msOpenMsiFileForReadWrite")(filename,&handle)!=0||!handle)throw std::runtime_error("MSI write open");
 if(sym<CloseImage>(image,"msFreeControlPointFileOfMSI")(handle)||sym<Allocate>(image,"msAllocControlPointFileOfMSI")(handle,unsigned(points.size())))throw std::runtime_error("GCP owned allocate/initialise");
 // Exact-build msAllocControlPointFileOfMSI allocates AND initializes this
 // owned buffer. +204 is the pointer used by its exported wrappers. Borrow
 // it only inside this process; native image close retains ownership. The
 // older SetControlPointFile path allocates an uninitialized destination and
 // then calls a copier that checks the destination's prior capacity field.
 memcpy(&controls,static_cast<char*>(handle)+204,sizeof controls);
 if(!controls)throw std::runtime_error("GCP owned buffer");
 using Enable=int(__cdecl*)(void*,unsigned,unsigned);
  for(unsigned i=0;i<points.size();++i){auto& q=points[i];if(sym<Set>(image,"msSetOneGCP")(controls,i+1,q.X,q.Y,q.x,q.y)!=0||sym<Enable>(image,"msSetOneGCPUseOnOff")(controls,i+1,1)!=0)throw std::runtime_error("GCP set/enable");}
 if(nativeCoordinates){
  using Value=int(__cdecl*)(void*,unsigned);using Calculate=int(__cdecl*)(void*);
  int kind=sym<Value>(image,"msSetCoordinateSysType")(handle,2),unit=sym<Value>(image,"msSetCoordinateUnit")(handle,3);
  int calculated=sym<Calculate>(image,"msCalSetUserCoorRangle")(handle);
  if(kind||unit||calculated){fprintf(stderr,"MSI extent kind=%d unit=%d calculation=%d\n",kind,unit,calculated);sym<CloseImage>(image,"msCloseMsiFileForReadWrite")(handle);throw std::runtime_error("MSI native coordinate extent initialization");}
 }
 image_gcps(image,handle);
 int closed=sym<CloseImage>(image,"msCloseMsiFileForReadWrite")(handle);
 printf("{\"event\":\"gcp_saved\",\"native_result\":%d}\n",closed);fflush(stdout);
 if(closed!=0)throw std::runtime_error("MSI GCP save");
}

struct RasterTiles {std::uint32_t type,width,height,bands,tileWidth,tileHeight;};
static_assert(sizeof(RasterTiles)==24,"raw tile protocol");
RasterTiles image_profile(HMODULE image,void* handle){
 using Get=int(__cdecl*)(void*,unsigned*);RasterTiles p{};unsigned bits=0,bandInfo=0;
 if(sym<Get>(image,"msGetLines")(handle,&p.height)||sym<Get>(image,"msGetLineCells")(handle,&p.width)||sym<Get>(image,"msGetBands")(handle,&p.bands)||sym<Get>(image,"msGetCellType")(handle,&p.type)||sym<int(__cdecl*)(unsigned,unsigned*)>(image,"msGetCellBitSize")(p.type,&bits)||sym<Get>(image,"msGetBandsInfo")(handle,&bandInfo))throw std::runtime_error("MSI native profile readback");
 using Coeff=int(__cdecl*)(void*,unsigned*,unsigned*);
 // The native API returns rows first and columns second.
 if(sym<Coeff>(image,"msGetTileCeff")(handle,&p.tileHeight,&p.tileWidth))throw std::runtime_error("MSI tile size readback");
 unsigned coordinates=0;int coordinateResult=sym<Get>(image,"msGetCoordinateSysType")(handle,&coordinates);
 using Range=int(__cdecl*)(void*,int,double*,double*,double*,double*);double xmin=0,xmax=0,ymin=0,ymax=0;
 int rangeResult=sym<Range>(image,"msGetUserCoordinateRangle")(handle,1,&xmin,&xmax,&ymin,&ymax);
 if(coordinateResult==0&&rangeResult==0&&std::isfinite(xmin)&&std::isfinite(xmax)&&std::isfinite(ymin)&&std::isfinite(ymax)){
  printf("{\"event\":\"image_extent\",\"coordinate_kind\":%u,\"native_bounds\":[%.17g,%.17g,%.17g,%.17g]}\n",coordinates,xmin,ymin,xmax,ymax);fflush(stdout);
 }
 printf("{\"event\":\"image_profile\",\"width\":%u,\"height\":%u,\"bands\":%u,\"cell_type\":%u,\"cell_bits\":%u,\"band_info\":%u,\"tile_width\":%u,\"tile_height\":%u}\n",p.width,p.height,p.bands,p.type,bits,bandInfo,p.tileWidth,p.tileHeight);fflush(stdout);return p;
}
void validate_raster_tiles(const RasterTiles& p){
 static const unsigned bytes[]={1,2,4,1,2,4,4,8};
 if(p.type>7||!p.width||!p.height||!p.bands||p.bands>32||!p.tileWidth||!p.tileHeight||p.tileWidth>128||p.tileHeight>128||p.tileWidth*p.tileHeight*bytes[p.type]!=16384)throw std::runtime_error("MSI typed tile header");
 auto tiles=((std::uint64_t(p.width)+p.tileWidth-1)/p.tileWidth)*((std::uint64_t(p.height)+p.tileHeight-1)/p.tileHeight)*p.bands;
 if(tiles*16384>2000000000ULL)throw std::runtime_error("MSI typed stream exceeds verified 2GB profile; use explicit raster tiles");
}
void image_raw(HMODULE image,const wchar_t* input,const wchar_t* output,bool importing){
 using Close=int(__cdecl*)(void*);
 struct Owned{void* handle=nullptr;Close drop=nullptr;~Owned(){if(handle)drop(handle);}void close(){auto p=handle;handle=nullptr;if(drop(p))throw std::runtime_error("MSI typed close");}} owned;
 auto in=ansi(input),out=ansi(output);RasterTiles header{};char magic[8]{};std::uint32_t hasNull=0;double nullValue=0;
 std::ifstream reader;std::ofstream writer;
 if(importing){
  reader.open(in.data(),std::ios::binary);reader.read(magic,8);reader.read(reinterpret_cast<char*>(&header),sizeof header);
  reader.read(reinterpret_cast<char*>(&hasNull),sizeof hasNull);reader.read(reinterpret_cast<char*>(&nullValue),sizeof nullValue);
  if(!reader||memcmp(magic,"MGRT0002",8)||hasNull>1||!std::isfinite(nullValue))throw std::runtime_error("MSI typed input header");validate_raster_tiles(header);
  auto position=reader.tellg();reader.seekg(0,std::ios::end);
  std::uint64_t tiles=((std::uint64_t(header.width)+header.tileWidth-1)/header.tileWidth)*((std::uint64_t(header.height)+header.tileHeight-1)/header.tileHeight)*header.bands;
  if(reader.tellg()!=std::streamoff(44+tiles*16384))throw std::runtime_error("MSI typed input size");reader.seekg(position);
  using Create=int(__cdecl*)(char*,void**,int,int,int,int,int);
  owned.drop=sym<Close>(image,"msCloseNewMsiFileForReadWrite");
  if(sym<Create>(image,"msCreateNewMsiFileForReadWrite")(out.data(),&owned.handle,int(header.type),int(header.height),int(header.width),int(header.bands),header.type==0&&header.bands==3?2:1)||!owned.handle)throw std::runtime_error("MSI typed create");
  auto actual=image_profile(image,owned.handle);if(memcmp(&header,&actual,sizeof header))throw std::runtime_error("MSI typed native layout differs");
  if(hasNull){
   using Enable=int(__cdecl*)(void*,unsigned);using Null=int(__cdecl*)(void*,double);
   if(sym<Enable>(image,"msSetHasNullCell")(owned.handle,1)||sym<Null>(image,"msSetNullCell")(owned.handle,nullValue))throw std::runtime_error("MSI native nodata marker");
  }
 }else{
  using Open=int(__cdecl*)(char*,void**);owned.drop=sym<Close>(image,"msCloseMsiFileForRead");
  if(sym<Open>(image,"msOpenMsiFileForRead")(in.data(),&owned.handle)||!owned.handle)throw std::runtime_error("MSI typed open");
  header=image_profile(image,owned.handle);validate_raster_tiles(header);image_gcps(image,owned.handle);
  using Get=int(__cdecl*)(void*,unsigned*);
  int nullResult=sym<Get>(image,"msGetHasNullCell")(owned.handle,&hasNull);
  if(nullResult!=0&&nullResult!=1000009)throw std::runtime_error("MSI typed null flag");
  if(hasNull){
   using Null=int(__cdecl*)(void*,double*);
   if(hasNull>1||sym<Null>(image,"msGetNullCell")(owned.handle,&nullValue)||!std::isfinite(nullValue))throw std::runtime_error("MSI native finite null-cell metadata");
  }
  writer.open(out.data(),std::ios::binary);writer.write("MGRT0002",8);writer.write(reinterpret_cast<char*>(&header),sizeof header);
  writer.write(reinterpret_cast<char*>(&hasNull),sizeof hasNull);writer.write(reinterpret_cast<char*>(&nullValue),sizeof nullValue);
  if(hasNull)printf("{\"event\":\"image_nodata\",\"enabled\":true,\"value\":%.17g}\n",nullValue);
  else puts("{\"event\":\"image_nodata\",\"enabled\":false,\"value\":null}");fflush(stdout);
 }
 using Tile=int(__cdecl*)(void*,int,int,int,int,void*);
 auto getTile=sym<Tile>(image,"msGetOneTileData"),setTile=sym<Tile>(image,"msSetOneTileData");
 auto transfer=importing?setTile:getTile;unsigned char data[16384],coarse[16384];
 using Get=int(__cdecl*)(void*,unsigned*);unsigned levels=0;
 if(sym<Get>(image,"msGetPyramidLayerNums")(owned.handle,&levels)||!levels||levels>31)throw std::runtime_error("MSI pyramid levels");
 unsigned rows=(header.height+header.tileHeight-1)/header.tileHeight,columns=(header.width+header.tileWidth-1)/header.tileWidth;
 for(unsigned band=0;band<header.bands;++band)for(unsigned row=0;row<rows;++row)for(unsigned col=0;col<columns;++col){
  if(importing){reader.read(reinterpret_cast<char*>(data),sizeof data);if(!reader)throw std::runtime_error("MSI typed tile input");}
  int transferred=transfer(owned.handle,0,int(band),int(row),int(col),data);
  if(transferred){fprintf(stderr,"MSI tile error=%d band=%u row=%u column=%u\n",transferred,band,row,col);throw std::runtime_error("MSI native tile transfer");}
  if(importing){
   static const unsigned bytes[]={1,2,4,1,2,4,4,8};unsigned size=bytes[header.type];
   unsigned top=row*header.tileHeight,left=col*header.tileWidth;
   unsigned bottom=(std::min)(top+header.tileHeight,header.height),right=(std::min)(left+header.tileWidth,header.width);
   for(unsigned level=1;level<levels;++level){
    unsigned step=1u<<level,firstRow=((top+step-1)/step)*step,firstCol=((left+step-1)/step)*step;
    if(firstRow>=bottom||firstCol>=right)continue;
    unsigned tileRow=(firstRow>>level)/header.tileHeight,tileCol=(firstCol>>level)/header.tileWidth;
    if(getTile(owned.handle,int(level),int(band),int(tileRow),int(tileCol),coarse))throw std::runtime_error("MSI pyramid tile read");
    for(unsigned y=firstRow;y<bottom;y+=step)for(unsigned x=firstCol;x<right;x+=step){
     unsigned source=((y-top)*header.tileWidth+x-left)*size;
     unsigned destination=(((y>>level)%header.tileHeight)*header.tileWidth+(x>>level)%header.tileWidth)*size;
     memcpy(coarse+destination,data+source,size);
    }
    if(setTile(owned.handle,int(level),int(band),int(tileRow),int(tileCol),coarse))throw std::runtime_error("MSI pyramid tile write");
   }
  }
  if(!importing){writer.write(reinterpret_cast<char*>(data),sizeof data);if(!writer)throw std::runtime_error("MSI typed tile output");}
 }
 if(!importing){
  using Cell=double(__cdecl*)(void*,int,int,int,int);auto cell=sym<Cell>(image,"msGetCell");
  using Size=int(__cdecl*)(void*,int,unsigned*,unsigned*);auto layerSize=sym<Size>(image,"msGetPyramidLayerSize");
  for(unsigned level=0;level<levels;++level)for(unsigned band=0;band<header.bands;++band){
   unsigned h=0,w=0;if(layerSize(owned.handle,int(level),&h,&w))throw std::runtime_error("MSI pyramid extent");
   unsigned xs[]={0,header.width/2,header.width-1},ys[]={0,header.height/2,header.height-1};
   printf("{\"event\":\"image_pyramid\",\"level\":%u,\"band\":%u,\"width\":%u,\"height\":%u,\"samples\":[",level,band,w,h);
   for(unsigned i=0;i<3;++i){double value=cell(owned.handle,int(level),int(band),int(ys[i]),int(xs[i]));if(!std::isfinite(value))throw std::runtime_error("MSI nonfinite sample requires explicit nodata mapping");printf("%s{\"pixel\":[%u,%u],\"value\":%.17g}",i?",":"",xs[i],ys[i],value);}
   puts("]}");fflush(stdout);
  }
 }
 if(!importing){writer.flush();if(!writer)throw std::runtime_error("MSI typed flush");writer.close();}
 owned.close();puts("{\"event\":\"image_closed\",\"native_result\":0}");fflush(stdout);
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
  if(wcscmp(argv[4],L"from_raw")==0||wcscmp(argv[4],L"to_raw")==0){
   bool importing=wcscmp(argv[4],L"from_raw")==0;image_raw(image,argv[5],argv[6],importing);result=0;
   if(importing&&argc==8)assign_image_gcps(image,output.data(),argv[7],true);
  }else if(wcscmp(argv[4],L"to_msi")==0){
   using Convert=int(__cdecl*)(char*,char*);result=sym<Convert>(image,"msTifToMsi")(input.data(),output.data());
   if(result==0&&argc==8)assign_image_gcps(image,output.data(),argv[7],true);
  }else if(wcscmp(argv[4],L"to_tiff")==0){
   using OpenImage=int(__cdecl*)(char*,void**);using CloseImage=int(__cdecl*)(void*);using Convert=int(__cdecl*)(void*,char*,unsigned);
   void* handle=nullptr;int opened=sym<OpenImage>(image,"msOpenMsiFileForRead")(input.data(),&handle);
   printf("{\"event\":\"image_opened\",\"native_result\":%d}\n",opened);fflush(stdout);
   if(opened!=0||!handle)throw std::runtime_error("MSI open failed");
   image_profile(image,handle);
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
  }else if(wcscmp(argv[2],L"inspect")==0||wcscmp(argv[2],L"plot")==0||wcscmp(argv[2],L"plot.ps")==0||wcscmp(argv[2],L"edit")==0){
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
  if(wcscmp(argv[2],L"edit")==0){
   if(argc!=7||GetFileAttributesW(argv[5])!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("project edit arguments");
   using GetItem=short(__stdcall*)(void*,short,void*);using Update=short(__stdcall*)(void*,short,void*,short);
   using State=short(__stdcall*)(void*,short,short);using Move=short(__stdcall*)(void*,short,short);
   auto controlName=ansi(argv[6]);std::ifstream control(controlName.data());int originals=0;
   if(!(control>>originals)||originals!=sym<Size>(projectLib,"_PRJGetFileNum")(project)||originals<1||originals>100)throw std::runtime_error("project original layer count");
   auto portableName=[](const std::string& name){if(name.empty()||name.size()>200||name.find_first_of("/\\:")!=std::string::npos||name=="."||name=="..")throw std::runtime_error("project edit layer name");};
   for(int i=0;i<originals;++i){
    std::string name;if(!(control>>name))throw std::runtime_error("project dependency list");portableName(name);
    unsigned char item[400]{};require(sym<GetItem>(projectLib,"_PRJGetItem")(project,short(i),item),"project existing item");
    memset(item+2,0,260);memcpy(item+2,name.c_str(),name.size());
    require(sym<Update>(projectLib,"_PRJUpdate")(project,short(i),item,0),"project clone reference");
   }
   struct Choice{int identity,state;std::string name;double minRate,maxRate;};std::vector<Choice> choices;std::set<int> keep;
   std::vector<int> current;for(int i=1;i<=originals;++i)current.push_back(i);
   int requested=0;if(!(control>>requested)||requested<1||requested>100)throw std::runtime_error("project desired layer count");
   for(int i=0;i<requested;++i){
    Choice c{};if(!(control>>c.identity>>c.state>>c.name>>c.minRate>>c.maxRate)||c.identity<0||c.identity>originals||c.state<0||c.state>3||!std::isfinite(c.minRate)||!std::isfinite(c.maxRate)||(c.maxRate>0&&c.minRate>c.maxRate))throw std::runtime_error("project edit choice");portableName(c.name);
    if(c.identity==0){
     auto path=ansi(std::wstring(c.name.begin(),c.name.end()).c_str());
     require(sym<Append>(projectLib,"_PRJAppend")(project,path.data(),nullptr,0),"project add layer");
     int index=sym<Size>(projectLib,"_PRJGetFileNum")(project)-1;unsigned char item[400]{};
     require(sym<GetItem>(projectLib,"_PRJGetItem")(project,short(index),item),"project new item");
     memset(item+2,0,260);memcpy(item+2,c.name.c_str(),c.name.size());
     require(sym<Update>(projectLib,"_PRJUpdate")(project,short(index),item,0),"project new relative path");
     c.identity=originals+i+1;current.push_back(c.identity);
    }
    if(!keep.insert(c.identity).second)throw std::runtime_error("project duplicate source index");choices.push_back(c);
   }
   ProjectEnvironment environment=project_environment(project);std::string extra;
   if(control>>extra){
    if(extra!="environment"||!(control>>environment.x>>environment.y>>environment.sx>>environment.sy>>environment.angle>>environment.width>>environment.height))throw std::runtime_error("project environment data");
    memcpy(static_cast<char*>(project)+2508,&environment,sizeof environment);project_environment(project);
    if(control>>extra)throw std::runtime_error("project trailing edit data");
   }
   for(int i=int(current.size())-1;i>=0;--i)if(!keep.count(current[i])){
    require(sym<Item>(projectLib,"_PRJDelete")(project,i),"project remove layer");current.erase(current.begin()+i);
   }
   for(int i=0;i<requested;++i){
    int found=i;while(found<int(current.size())&&current[found]!=choices[i].identity)++found;
    if(found==int(current.size()))throw std::runtime_error("project reorder identity");
    if(found!=i){if(sym<Move>(projectLib,"_PRJMoveItem")(project,short(found),short(i))!=i)throw std::runtime_error("project reorder");int id=current[found];current.erase(current.begin()+found);current.insert(current.begin()+i,id);}
    if(sym<State>(projectLib,"_PRJSetNewState")(project,short(i),short(choices[i].state))<0)throw std::runtime_error("project state update");
    using Rate=short(__stdcall*)(void*,short,double*,double*);
    require(sym<Rate>(projectLib,"_PRJSetItemDispRate")(project,short(i),&choices[i].minRate,&choices[i].maxRate),"project display-limit update");
   }
   for(int i=0;i<requested;++i)if(sym<Item>(projectLib,"_PRJGetItemState")(project,i)!=choices[i].state)throw std::runtime_error("project current-state conflict");
   auto output=ansi(argv[5]);sym<Rename>(projectLib,"_PRJSetName")(project,output.data());
   require(sym<Save>(projectLib,"_PRJSaveAsProject")(project,output.data()),"project edited save");
  }
  auto environment=project_environment(project);project_environment_event(environment);
  int count=sym<Size>(projectLib,"_PRJGetFileNum")(project);if(count<1||count>100)throw std::runtime_error("project loaded layer count");
  printf("{\"event\":\"project\",\"layers\":%d}\n",count);fflush(stdout);
  bool postscript=wcscmp(argv[2],L"plot.ps")==0;bool plotting=postscript||wcscmp(argv[2],L"plot")==0;Drawing drawing;HMODULE dsp=nullptr;
  if(plotting){
   if(argc!=7&&argc!=8)throw std::runtime_error("project plot arguments");auto rendererPath=program+L"/winout60.dll",dspPath=program+L"/mapdsp60.dll";
   if(digest(rendererPath.c_str())!="faa4f793b73e9e9e54a90982028bcca9d4b93c1453ebada63766e17f92ebfd2f"||digest(dspPath.c_str())!="e1995ea477c7d87bd6ed75d7327376b9a5f9ed8d2564b0a42dbada9197b671cb")throw std::runtime_error("project renderer profile");
   HMODULE renderer=LoadLibraryW(rendererPath.c_str());dsp=GetModuleHandleW(L"mapdsp60.dll");if(!renderer||!dsp)throw std::runtime_error("project renderer initialization");if(argc==8)bind_system_library(library,dsp,argv[7]);begin_eps(drawing,renderer,dsp,argv[5],argv[6],postscript);compose_project_environment(drawing,dsp,environment);
  }
  std::wstring actualProject(wcscmp(argv[2],L"create")==0?argv[5]:argv[4]);auto actualRoot=actualProject.substr(0,actualProject.find_last_of(L"/\\"));
  for(int i=0;i<count;++i){
   short kind=sym<Item>(projectLib,"_PRJGetItemType")(project,i);if((kind<0||kind>2)&&kind!=11)throw std::runtime_error("project layer type");
   short state=sym<Item>(projectLib,"_PRJGetItemState")(project,i);
   using Rate=short(__stdcall*)(void*,short,double*,double*);double minRate=0,maxRate=0;
   require(sym<Rate>(projectLib,"_PRJGetItemDispRate")(project,short(i),&minRate,&maxRate),"project display limits");
   if(!std::isfinite(minRate)||!std::isfinite(maxRate))throw std::runtime_error("project nonfinite display limits");
   bool visible=state!=0&&(!plotting||(minRate<=0&&maxRate<=0)||(drawing.scale>=minRate&&(maxRate<=0||drawing.scale<=maxRate)));
   printf("{\"event\":\"project_state\",\"index\":%d,\"state\":%d,\"display_limits\":[%.17g,%.17g]}\n",i+1,state,minRate,maxRate);fflush(stdout);
   auto name=sym<ItemName>(projectLib,"_PRJGetItemName")(project,i);if(!name||strlen(name)>260)throw std::runtime_error("project layer path");
   std::string hex;for(unsigned char c:std::string(name)){char pair[3];sprintf_s(pair,"%02x",c);hex+=pair;}
   if(!SetCurrentDirectoryW(actualRoot.c_str()))throw std::runtime_error("project layer cwd");
   if(kind==11){
    using State=short(__stdcall*)(void*,short,short);
    // sSetNewState(0) releases this item's CMsiDB. Temporarily open only
    // this owned in-memory item for range readback, then restore its state;
    // the source/project is never saved with the temporary visible state.
    if(state==0&&sym<State>(projectLib,"_PRJSetNewState")(project,short(i),1)<0)throw std::runtime_error("project hidden MSI inspect open");
    using Find=void*(__thiscall*)(void*,short);using Bounds=short(__thiscall*)(void*,RectD*);
    void* item=sym<Find>(projectLib,"?FindItem@MapProject@@QAEPAVBaseItem@@F@Z")(project,short(i));RectD range{};
    if(!item||sym<Bounds>(projectLib,"?GetRect@msiImageItem@@UAEFPAUD_RECT@@@Z")(item,&range)<=0||!std::isfinite(range.xmin)||!std::isfinite(range.ymin)||!std::isfinite(range.xmax)||!std::isfinite(range.ymax)||range.xmin>=range.xmax||range.ymin>=range.ymax)throw std::runtime_error("project MSI native extent");
    if(state==0&&sym<State>(projectLib,"_PRJSetNewState")(project,short(i),0)<0)throw std::runtime_error("project hidden MSI inspect restore");
    printf("{\"event\":\"project_layer\",\"index\":%d,\"kind\":11,\"active\":1,\"native_bounds\":[%.17g,%.17g,%.17g,%.17g],\"path_gbk_hex\":\"%s\"}\n",i+1,range.xmin,range.ymin,range.xmax,range.ymax,hex.c_str());fflush(stdout);
    if(plotting&&visible){using Display=short(__thiscall*)(void*,void*);require(sym<Display>(projectLib,"?Display@msiImageItem@@UAEFPAVKernelDriver@@@Z")(item,drawing.dc),"project MSI drawing");printf("{\"event\":\"layer_drawn\",\"index\":%d,\"kind\":11,\"records\":1}\n",i);fflush(stdout);}continue;
   }
   using OpenItem=short(__thiscall*)(void*,short);require(sym<OpenItem>(projectLib,"?sOpenItemArea@MapProject@@QAEFF@Z")(project,short(i)),"project layer open");
   Area area{};area.area=sym<Item>(projectLib,"_PRJGetItemArea")(project,i);area.open=1;
   auto countFn=sym<Count>(library,kind==0?"?_GetLinNum@C60_WorkArea@@QAEFPAJ0@Z":kind==1?"?_GetPntNum@C60_WorkArea@@QAEFPAJ0@Z":"?_GetRegNum@C60_WorkArea@@QAEFPAJ0@Z");
   long active=-1,upper=-1;require(countFn(&area,&active,&upper),"project layer count readback");
   printf("{\"event\":\"project_layer\",\"index\":%d,\"kind\":%d,\"area\":%d,\"active\":%ld,\"path_gbk_hex\":\"%s\"}\n",i+1,kind,area.area,active,hex.c_str());fflush(stdout);
   using Find=void*(__thiscall*)(void*,short);using Bounds=short(__thiscall*)(void*,RectD*);
   void* item=sym<Find>(projectLib,"?FindItem@MapProject@@QAEPAVBaseItem@@F@Z")(project,short(i));RectD nativeBounds{};
   short bounded=item?sym<Bounds>(projectLib,"?GetRect@BaseItem@@UAEFPAUD_RECT@@@Z")(item,&nativeBounds):0;
   if(bounded>0&&std::isfinite(nativeBounds.xmin)&&std::isfinite(nativeBounds.ymin)&&std::isfinite(nativeBounds.xmax)&&std::isfinite(nativeBounds.ymax)&&nativeBounds.xmin<=nativeBounds.xmax&&nativeBounds.ymin<=nativeBounds.ymax){
    printf("{\"event\":\"project_layer_bounds\",\"index\":%d,\"native_bounds\":[%.17g,%.17g,%.17g,%.17g]}\n",i+1,nativeBounds.xmin,nativeBounds.ymin,nativeBounds.xmax,nativeBounds.ymax);fflush(stdout);
   }
   if(plotting&&visible){
    if(kind==2){
     if(upper>1000000)throw std::runtime_error("project plot region bound");using Read=short(__stdcall*)(int,long,long**,long*,void*);using Draw=short(__stdcall*)(void*,short,long*,long,void*,long);
     auto get=sym<Read>(library,MAKEINTRESOURCEA(44));auto draw=sym<Draw>(dsp,"?_DispReg@@YGFPAVKernelDriver@@FPAJJPAUREG_INFOstruct@@J@Z");long drawn=0;
     for(long id=1;id<=upper;++id){long* topology=nullptr;long length=0;unsigned char info[128]{};if(get(area.area,id,&topology,&length,info)<=0)continue;require(draw(drawing.dc,area.area,topology,length,info,0),"project region drawing");++drawn;}if(drawn!=active)throw std::runtime_error("project region drawing count");
    }else{using Draw=short(__stdcall*)(void*,short,short);require(sym<Draw>(dsp,kind==0?"_DispLinArea":"_DispPntArea")(drawing.dc,area.area,0),"project vector drawing");}
    printf("{\"event\":\"layer_drawn\",\"index\":%d,\"kind\":%d,\"records\":%ld}\n",i,kind,active);fflush(stdout);
   }
   normalize_empty_attributes(library,area);
  }
  using ProjectBounds=void(__thiscall*)(void*,RectD*);RectD range{};
  sym<ProjectBounds>(projectLib,"?vGetRange@MapProject@@QAEXPAUD_RECT@@@Z")(project,&range);
  if(std::isfinite(range.xmin)&&std::isfinite(range.ymin)&&std::isfinite(range.xmax)&&std::isfinite(range.ymax)&&range.xmin<=range.xmax&&range.ymin<=range.ymax){
   printf("{\"event\":\"project_bounds\",\"native_bounds\":[%.17g,%.17g,%.17g,%.17g]}\n",range.xmin,range.ymin,range.xmax,range.ymax);fflush(stdout);
  }
  if(plotting){drawing.destroy(drawing.dc);drawing.dc=nullptr;printf("{\"event\":\"plot_saved\",\"sha256\":\"%s\"}\n",digest(argv[6]).c_str());fflush(stdout);}
  sym<Flag>(projectLib,"_PRJSetModifyFlg")(project,0);using Delete=short(__stdcall*)(void*);
  short closed=sym<Delete>(projectLib,"_PRJDeleteProject")(project);if(closed==2)throw std::runtime_error("project close refused");
  puts("{\"event\":\"project_closed\"}");puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const std::exception&e){fprintf(stderr,"%s\n",e.what());return 1;}
}

#include "NativeTerrain.h"
#include "NativeScene.h"
#include "NativeTinScene.h"
#include "NativeMsd.h"
#include "NativeCom.h"
#include "NativeSdk.h"

int wmain(int argc,wchar_t**argv){
 SetUnhandledExceptionFilter(native_failure);
 try{bind_private_desktop(argc==7&&(!wcscmp(argv[1],L"scene")||!wcscmp(argv[1],L"tin.scene")));}catch(const std::exception&e){fprintf(stderr,"%s\n",e.what());return 1;}
 if(argc==5&&wcscmp(argv[1],L"sdk")==0)return native_sdk::run(argv);
 if(argc==7&&wcscmp(argv[1],L"tin.scene")==0)return native_tin_scene::run(argv);
 if(argc==7&&wcscmp(argv[1],L"scene")==0)return native_scene::run(argv);
 if(argc==6&&wcscmp(argv[1],L"terrain")==0)return terrain_main(argv);
 if((argc==7||argc==8)&&wcscmp(argv[1],L"msd")==0)return msd_main(argc,argv);
 if(argc==8&&wcscmp(argv[1],L"com")==0)return com_main(argc,argv);
 if((argc==7||argc==8)&&wcscmp(argv[1],L"image")==0)return image_main(argc,argv);
 if((argc==6||argc==7||argc==8)&&wcscmp(argv[1],L"project")==0)return project_main(argc,argv);
 if((argc==6||argc==7)&&(wcscmp(argv[1],L"plot")==0||wcscmp(argv[1],L"plot.ps")==0))return plot_main(argc,argv);
 if(argc==7&&wcscmp(argv[1],L"workspace")==0)return workspace_main(argv);
 // op, DLL, input, native kind (0 line/1 point/2 polygon), output or -, command file or -.
 if(argc!=7)return 2;SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
 try{
  std::wstring op(argv[1]);if(op!=L"inspect"&&op!=L"resave"&&op!=L"point.move"&&op!=L"point.radius"&&op!=L"point.style"&&op!=L"point.text"&&op!=L"line.replace"&&op!=L"arc.replace"&&op!=L"node.move"&&op!=L"feature.delete")throw std::runtime_error("unsupported operation");
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
  if(op==L"line.replace"||op==L"arc.replace"||op==L"node.move"||op==L"point.radius"||op==L"point.style"||op==L"point.text"){
   auto command=ansi(argv[6]);std::ifstream stream(command.data());if(!stream)throw std::runtime_error("command file");edit_area(s,kind,op,stream);
  }
  if(op==L"feature.delete"){
   PreserveAttributes preserve(s);
   NativeAttributeDataOrigin origin(s,kind);
   using Del=short(__thiscall*)(Area*,long);auto del=sym<Del>(s.library,kind==2?"?_DelReg@C60_WorkArea@@QAEFJ@Z":kind==1?"?_DelPnt@C60_WorkArea@@QAEFJ@Z":"?_DelLin@C60_WorkArea@@QAEFJ@Z");auto command=ansi(argv[6]);std::ifstream stream(command.data());long id;int n=0;
   while(stream>>id){if(++n>1000||id<1)throw std::runtime_error("delete IDs");require(del(&s.area,id),"native deletion failed");printf("{\"event\":\"feature_deleted\",\"record_id\":%ld}\n",id);fflush(stdout);}if(!n||!stream.eof())throw std::runtime_error("delete command data");
  }
  if(op!=L"inspect"){
   if(GetFileAttributesW(argv[5])!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("output exists");
   auto out=ansi(argv[5]);auto save=sym<File>(s.library,"?_SaveAFile@C60_WorkArea@@QAEFPAD000@Z");require(save(&s.area,out.data(),nullptr,nullptr,nullptr),"save failed");counts(&s.area,count,"saved");puts("{\"event\":\"saved\"}");fflush(stdout);
  }
  s.close();puts("{\"event\":\"area_closed\"}");
  puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const std::exception&e){fprintf(stderr,"%s\n",e.what());return 1;}
}
