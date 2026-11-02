// Installed SDK automation, confined to this owned x86 process and desktop.
// Normal class factories and licensing remain in force; no registry writes.
#pragma once
#include <ole2.h>
#include <comdef.h>
#pragma comment(lib,"ole32.lib")
#pragma comment(lib,"oleaut32.lib")
#pragma comment(lib,"uuid.lib")
#pragma comment(lib,"comsuppw.lib")

using ComDispatch=_com_ptr_t<_com_IIID<IDispatch,&IID_IDispatch>>;
using ComFactory=_com_ptr_t<_com_IIID<IClassFactory,&IID_IClassFactory>>;

void com_require(HRESULT hr,const char* action){
 if(FAILED(hr)){char message[180];sprintf_s(message,"COM %s: HRESULT 0x%08lx",action,hr);throw std::runtime_error(message);}
}

_variant_t com_invoke(IDispatch* object,const wchar_t* name,WORD flags,std::initializer_list<_variant_t> arguments={}){
 LPOLESTR member=const_cast<LPOLESTR>(name);DISPID id=0;
 com_require(object->GetIDsOfNames(IID_NULL,&member,1,LOCALE_USER_DEFAULT,&id),"member lookup");
 // The initializer list owns the values until Invoke returns. VARIANTARG here
 // borrows them in Automation's required reverse order, without another owner.
 std::vector<VARIANTARG> reversed;for(auto& argument:arguments)reversed.push_back(static_cast<const VARIANT&>(argument));
 std::reverse(reversed.begin(),reversed.end());DISPID put=DISPID_PROPERTYPUT;
 DISPPARAMS params{};params.cArgs=static_cast<UINT>(reversed.size());params.rgvarg=reversed.data();
 if(flags&(DISPATCH_PROPERTYPUT|DISPATCH_PROPERTYPUTREF)){params.cNamedArgs=1;params.rgdispidNamedArgs=&put;}
 _variant_t result;EXCEPINFO info{};UINT argument=0;
 HRESULT hr=object->Invoke(id,IID_NULL,LOCALE_USER_DEFAULT,flags,&params,&result,&info,&argument);
 if(info.bstrSource)SysFreeString(info.bstrSource);if(info.bstrDescription)SysFreeString(info.bstrDescription);if(info.bstrHelpFile)SysFreeString(info.bstrHelpFile);
 if(FAILED(hr)){fprintf(stderr,"COM member %ls, reversed argument %u\n",name,argument);}
 com_require(hr,"invoke");return result;
}


long com_long(const _variant_t& value){
 if(value.vt==VT_I2)return value.iVal;if(value.vt==VT_I4)return value.lVal;
 throw std::runtime_error("COM non-integer result");
}
bool com_bool(const _variant_t& value){if(value.vt!=VT_BOOL)throw std::runtime_error("COM non-boolean result");return value.boolVal!=VARIANT_FALSE;}
void com_stage(const char* name){printf("{\"event\":\"com_stage\",\"name\":\"%s\"}\n",name);fflush(stdout);}

struct ComContext{
 HANDLE context=INVALID_HANDLE_VALUE;ULONG_PTR cookie=0;bool initialized=false;std::wstring runtime;
 ComContext(const wchar_t* bas,const wchar_t* manifest){
  if(digest(bas)!=BAS_SHA)throw std::runtime_error("COM BAS build profile");
  std::wstring filename(manifest);runtime=filename.substr(0,filename.find_last_of(L"/\\"));
  const wchar_t* names[]={L"MapGisBasCom1.DLL",L"MapGisMapCom.dll",L"MapgisLayerCom.dll",L"MapGisDspCom.dll"};
  const char* hashes[]={"dbad827ed7e1586d358a8d56ec8873be3f3a7ab005797c3c57460636747077d2","f01519451e70e2a1ae4c419d7e4f42cfe447a85afcb8cda5a8d1449c248562df","b9daa169cddaed2b26e7f20944a56088fb3fbebdf51b9c7c396f143483d90a2c","af330ddf5320fa6bf1f0a4c915a188664e2e21c345c861aaa3c16205035f09aa"};
  for(int i=0;i<4;++i)if(digest((runtime+L"/"+names[i]).c_str())!=hashes[i])throw std::runtime_error("COM SDK build profile");
  ACTCTXW settings{};settings.cbSize=sizeof settings;settings.lpSource=manifest;context=CreateActCtxW(&settings);
  if(context==INVALID_HANDLE_VALUE)throw std::runtime_error("COM private activation manifest");
  if(!ActivateActCtx(context,&cookie)){ReleaseActCtx(context);context=INVALID_HANDLE_VALUE;throw std::runtime_error("COM private activation context");}
  HRESULT hr=OleInitialize(nullptr);
  if(FAILED(hr)){DeactivateActCtx(0,cookie);ReleaseActCtx(context);cookie=0;context=INVALID_HANDLE_VALUE;com_require(hr,"initialization");}
  initialized=true;std::wstring program(bas);SetDllDirectoryW(program.substr(0,program.find_last_of(L"/\\")).c_str());
 }
 ~ComContext(){if(initialized)OleUninitialize();if(cookie)DeactivateActCtx(0,cookie);if(context!=INVALID_HANDLE_VALUE)ReleaseActCtx(context);}
 ComDispatch create(const wchar_t* library,const wchar_t* id){
  HMODULE module=LoadLibraryW((runtime+L"/"+library).c_str());
  if(!module){DWORD code=GetLastError();char message[200];sprintf_s(message,"COM original module %ls initialization: Win32 %lu",library,code);throw std::runtime_error(message);}
  using Factory=HRESULT(__stdcall*)(REFCLSID,REFIID,void**);CLSID cls{};com_require(CLSIDFromString(id,&cls),"class identity");
  ComFactory factory;com_require(sym<Factory>(module,"DllGetClassObject")(cls,IID_IClassFactory,reinterpret_cast<void**>(&factory)),"class factory");
  ComDispatch object;com_require(factory->CreateInstance(nullptr,IID_IDispatch,reinterpret_cast<void**>(&object)),"creation");return object;
 }
};

const wchar_t* const COM_MAP_FIELDS[]={L"type",L"unit",L"mapw",L"maph",L"projType",L"earthParam",L"jd0",L"wd0",L"djd",L"dwd",L"rate",L"lon",L"lon1",L"lon2",L"lat",L"lat1",L"lat2",L"dx",L"dy",L"levelType",L"h",L"H2",L"vRate",L"vUnit",L"TICtype",L"TICunit",L"infoUnit",L"infoRatex",L"infoRatey"};
bool com_map_integer(size_t index){return index==0||index==1||index==4||index==5||index==19||index==23||index==24||index==25||index==26;}
void com_map_parameters_event(IDispatch* parameters,const char* stage){
 printf("{\"event\":\"com_project_parameters\",\"stage\":\"%s\",\"values\":{",stage);
 for(size_t i=0;i<_countof(COM_MAP_FIELDS);++i){
  auto value=com_invoke(parameters,COM_MAP_FIELDS[i],DISPATCH_PROPERTYGET);double number=0;
  if(com_map_integer(i))number=com_long(value);else{if(value.vt!=VT_R8)throw std::runtime_error("COM map parameter numeric type");number=value.dblVal;}
  if(!std::isfinite(number))throw std::runtime_error("COM nonfinite map parameter");
  printf("%s\"%ls\":%.17g",i?",":"",COM_MAP_FIELDS[i],number);
 }
 puts("}}");fflush(stdout);
}

void com_apply_map_parameters(IDispatch* parameters,const wchar_t* controlPath){
 auto controlName=ansi(controlPath);std::ifstream control(controlName.data());int n=0;
 if(!(control>>n)||n<1||n>_countof(COM_MAP_FIELDS))throw std::runtime_error("COM map parameter change count");
 std::set<std::string> unique;
 for(int i=0;i<n;++i){std::string name;double value=0;if(!(control>>name>>value)||!std::isfinite(value)||!unique.insert(name).second)throw std::runtime_error("COM map parameter change");
  size_t index=0;std::wstring wide(name.begin(),name.end());while(index<_countof(COM_MAP_FIELDS)&&wide!=COM_MAP_FIELDS[index])++index;
  if(index==_countof(COM_MAP_FIELDS))throw std::runtime_error("COM unknown map parameter");
  if(com_map_integer(index)){if(value!=std::floor(value)||value<0||value>100)throw std::runtime_error("COM map parameter enum");com_invoke(parameters,wide.c_str(),DISPATCH_PROPERTYPUT,{_variant_t(long(value))});}
  else com_invoke(parameters,wide.c_str(),DISPATCH_PROPERTYPUT,{_variant_t(value)});
 }
 std::string trailing;if(control>>trailing)throw std::runtime_error("COM map parameter trailing data");
}


// Exact MapProject header ABI. readproject copies its 1101-byte saved header
// into object+1766; the 169-byte MAP_PARA starts at header+804 (file+816).
// BasCom1's own Get/SetFileMapParam wrappers use IMap_Para interface+8 for
// that same packed SDK structure. All involved DLL hashes are pinned.
void com_native_project_parameters(wchar_t** argv,bool editing){
 ComContext context(argv[2],argv[3]);std::wstring program(argv[2]);program=program.substr(0,program.find_last_of(L"/\\"));
 std::wstring path=program+L"/mapprj60.dll";
 if(digest(path.c_str())!="faeeddbe9d4190f6b3645bfb0610a60bd68a6724f03d1e638932939f083f3291")throw std::runtime_error("COM project header build profile");
 HMODULE library=LoadLibraryW(path.c_str());if(!library)throw std::runtime_error("COM native project initialization");
 using Create=void*(__stdcall*)(short);using Flag=short(__stdcall*)(void*,short);using Name=void(__stdcall*)(void*,char*);
 using Read=void(__thiscall*)(void*,char*);using Save=short(__stdcall*)(void*,char*);using Size=short(__stdcall*)(void*);using Close=short(__stdcall*)(void*);
 void* project=sym<Create>(library,"_PRJCreateProject")(0);if(!project)throw std::runtime_error("COM native project allocation");
 auto close=sym<Close>(library,"_PRJDeleteProject");
 try{
  sym<Flag>(library,"_PRJSetDispMsgFlag")(project,0);std::wstring source(argv[5]);auto split=source.find_last_of(L"/\\");
  if(split==std::wstring::npos||GetFileAttributesW(argv[5])==INVALID_FILE_ATTRIBUTES||!SetCurrentDirectoryW(source.substr(0,split).c_str()))throw std::runtime_error("COM native project input");
  auto full=ansi(argv[5]),leaf=ansi(source.substr(split+1).c_str());sym<Name>(library,"_PRJSetName")(project,full.data());
  sym<Read>(library,"?readproject@MapProject@@UAEXPAD@Z")(project,leaf.data());
  long count=sym<Size>(library,"_PRJGetFileNum")(project);if(count<1||count>100)throw std::runtime_error("COM native project layer count");
  auto parameters=context.create(L"MapGisBasCom1.DLL",L"{86167651-8AE5-4C1D-8910-42421077E919}");
  auto native=static_cast<BYTE*>(project)+2570;auto values=reinterpret_cast<BYTE*>(parameters.GetInterfacePtr())+8;
  memcpy(values,native,169);com_map_parameters_event(parameters,"before");
  if(editing){
   if(GetFileAttributesW(argv[6])!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("COM native project output exists");
   com_apply_map_parameters(parameters,argv[7]);memcpy(native,values,169);com_map_parameters_event(parameters,"after");
   auto output=ansi(argv[6]);sym<Name>(library,"_PRJSetName")(project,output.data());
   sym<Flag>(library,"_PRJSetModifyFlg")(project,1);
   require(sym<Save>(library,"_PRJSaveAsProject")(project,output.data()),"COM native project parameter save");
   sym<Flag>(library,"_PRJSetModifyFlg")(project,0);
   printf("{\"event\":\"com_project_parameters_saved\",\"layers\":%ld,\"sha256\":\"%s\"}\n",count,digest(argv[6]).c_str());fflush(stdout);
  }
  sym<Flag>(library,"_PRJSetModifyFlg")(project,0);short closed=close(project);project=nullptr;
  if(closed==2)throw std::runtime_error("COM native project close refused");
  printf("{\"event\":\"com_project_layers\",\"layers\":%ld}\n",count);puts("{\"event\":\"complete\"}");fflush(stdout);
 }catch(...){if(project){sym<Flag>(library,"_PRJSetModifyFlg")(project,0);close(project);}throw;}
}

#include "NativeLegend.h"

int com_main(int argc,wchar_t** argv){
 SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
 try{
  if(argc!=8)throw std::runtime_error("COM operation arguments");
  DWORD dep=0;BOOL permanent=FALSE;if(!GetProcessDEPPolicy(GetCurrentProcess(),&dep,&permanent)||dep)throw std::runtime_error("COM owned legacy compatibility");
  printf("{\"event\":\"host\",\"protocol\":1,\"pid\":%lu,\"bits\":32,\"dep_flags\":%lu,\"desktop\":\"%s\"}\n",GetCurrentProcessId(),dep,desktop_name().c_str());fflush(stdout);
  if(wcscmp(argv[4],L"project.params.native.inspect")==0||wcscmp(argv[4],L"project.params.native.edit")==0){com_native_project_parameters(argv,wcscmp(argv[4],L"project.params.native.edit")==0);return 0;}
  if(wcscmp(argv[4],L"legend.create")==0||wcscmp(argv[4],L"legend.inspect")==0){com_legend(argv,wcscmp(argv[4],L"legend.create")==0);return 0;}
  bool appending=wcscmp(argv[4],L"append")==0;
  if(!appending&&wcscmp(argv[4],L"subset")!=0)throw std::runtime_error("COM operation arguments");
  if(GetFileAttributesW(argv[6])!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("COM output exists");
  auto control=ansi(argv[7]);std::ifstream input(control.data());int kind=-1,n=0;
  if(!(input>>kind>>n)||kind<0||kind>2||n<1||n>200000)throw std::runtime_error("COM selection header");
  std::vector<long> records;std::set<long> unique;for(int i=0;i<n;++i){long id=0;if(!(input>>id)||id<1||!unique.insert(id).second)throw std::runtime_error("COM selection identity");records.push_back(id);}
  std::string trailing,base;bool transforming=false;double sx=1,sy=1,angle=0,dx=0,dy=0;short order[3]{};
  if(appending){
   if(!(input>>trailing>>base)||trailing!="base"||base.empty()||base.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789-_.")!=std::string::npos||base=="."||base==".."||input>>trailing)throw std::runtime_error("COM append base filename");
  }else if(input>>trailing){
   if(trailing!="transform"||!(input>>sx>>sy>>angle>>dx>>dy>>order[0]>>order[1]>>order[2])||sx==0||sy==0)throw std::runtime_error("COM selection transform control");
   for(double value:{sx,sy,angle,dx,dy})if(!std::isfinite(value))throw std::runtime_error("COM selection transform finite");
   if(std::set<short>(order,order+3)!=std::set<short>({1,2,3})||input>>trailing)throw std::runtime_error("COM selection transform order");transforming=true;
  }
  com_stage("selection_control_read");ComContext context(argv[2],argv[3]);com_stage("sdk_initialized");
  const wchar_t* areas[]={L"{C18776C2-9AF5-4A6F-88C9-7F96F2E6DDF8}",L"{815B7C63-500E-4D86-BFFD-35677B9CEC53}",L"{6F5430C6-5A6D-4D4C-88C4-CAFFAFF66AF1}"};
  auto source=context.create(L"MapGisBasCom1.DLL",areas[kind]),target=context.create(L"MapGisBasCom1.DLL",areas[kind]);com_stage("areas_created");
  for(IDispatch* area:{source.GetInterfacePtr(),target.GetInterfacePtr()}){
   com_invoke(area,L"ShowErrMsg",DISPATCH_PROPERTYPUT,{_variant_t(false)});com_invoke(area,L"Warning",DISPATCH_PROPERTYPUT,{_variant_t(false)});
  }
  if(!com_bool(com_invoke(source,L"Load",DISPATCH_METHOD,{_variant_t(argv[5]),_variant_t(L""),_variant_t(L""),_variant_t(L"")})))throw std::runtime_error("COM native input load");com_stage("source_loaded");
  long initial=0;
  if(appending){
   std::wstring folder(argv[5]);folder=folder.substr(0,folder.find_last_of(L"/\\"));std::wstring filename=folder+L"/"+std::wstring(base.begin(),base.end());
   if(!com_bool(com_invoke(target,L"Load",DISPATCH_METHOD,{_variant_t(filename.c_str()),_variant_t(L""),_variant_t(L""),_variant_t(L"")})))throw std::runtime_error("COM append base load");
   initial=com_long(com_invoke(target,L"logCount",DISPATCH_PROPERTYGET));com_stage("base_loaded");
  }
  auto list=context.create(L"MapGisBasCom1.DLL",L"{79F124E7-8354-4294-AC76-2F0EF13710A8}");
  for(long id:records){if(com_long(com_invoke(source,L"GetExistFlag",DISPATCH_METHOD,{_variant_t(id)}))!=1)throw std::runtime_error("COM missing selected entity");com_invoke(list,L"Append",DISPATCH_METHOD,{_variant_t(id)});}
  com_stage("selection_validated");if(!appending&&!com_bool(com_invoke(source,L"CopyMapParam",DISPATCH_METHOD,{_variant_t(target.GetInterfacePtr())})))throw std::runtime_error("COM copy map parameters");com_stage("map_parameters_copied");
  long copied=com_long(com_invoke(source,L"CopyList",DISPATCH_METHOD,{_variant_t(list.GetInterfacePtr()),_variant_t(target.GetInterfacePtr()),_variant_t(short(1))}));
  com_stage("copy_completed");if(copied!=n||com_long(com_invoke(target,L"logCount",DISPATCH_PROPERTYGET))!=initial+n)throw std::runtime_error("COM copy selection count");com_stage("copy_count_checked");
  for(int i=0;i<n;++i){long output=com_long(com_invoke(list,L"item",DISPATCH_PROPERTYGET,{_variant_t(long(i))}));
   printf("{\"event\":\"com_entity_mapping\",\"source_record_id\":%ld,\"output_record_id\":%ld}\n",records[i],output);}
  com_stage("mapping_read");if(transforming){
   auto transform=context.create(L"MapGisBasCom1.DLL",L"{82C0FAD5-05CC-4CD1-BC8A-0D8F9390C950}");
   for(short i=0;i<3;++i)com_invoke(transform,L"type",DISPATCH_PROPERTYPUT,{_variant_t(i),_variant_t(long(order[i]))});
   for(auto& field:std::initializer_list<std::pair<const wchar_t*,double>>{{L"enlarge_rx",sx},{L"enlarge_ry",sy},{L"enlarge_rz",1.},{L"rotate_ang",angle},{L"move_dx",dx},{L"move_dy",dy},{L"move_dz",0.}})com_invoke(transform,field.first,DISPATCH_PROPERTYPUT,{_variant_t(field.second)});
   bool success=kind==1?com_bool(com_invoke(target,L"Transform",DISPATCH_METHOD,{_variant_t(transform.GetInterfacePtr()),_variant_t(short(0))})):com_bool(com_invoke(target,L"Transform",DISPATCH_METHOD,{_variant_t(transform.GetInterfacePtr())}));
   if(!success)throw std::runtime_error("COM selection native transform");puts("{\"event\":\"com_subset_transformed\"}");fflush(stdout);
  }
  com_stage("before_dirty_read");bool before=com_bool(com_invoke(target,L"Changed",DISPATCH_PROPERTYGET));com_stage("before_save");
  if(!com_bool(com_invoke(target,L"Save",DISPATCH_METHOD,{_variant_t(argv[6]),_variant_t(L""),_variant_t(L""),_variant_t(L"")})))throw std::runtime_error("COM native save");
  bool after=com_bool(com_invoke(target,L"Changed",DISPATCH_PROPERTYGET));
  printf("{\"event\":\"%s\",\"count\":%d,\"base_count\":%ld,\"total_count\":%ld,\"dirty_before\":%s,\"dirty_after\":%s,\"sha256\":\"%s\"}\n",appending?"com_append_saved":"com_subset_saved",n,initial,initial+n,before?"true":"false",after?"true":"false",digest(argv[6]).c_str());
  puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const _com_error& e){fprintf(stderr,"COM Automation error 0x%08lx\n",e.Error());return 1;}
 catch(const std::exception& e){fprintf(stderr,"%s\n",e.what());return 1;}
}
