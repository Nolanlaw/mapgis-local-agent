// Exact installed MapGIS 6.7 legend adapter. All writes are to owned objects
// and files. The original C API avoids broken COM Add/Type/SaveAs adapters.
#pragma once

struct LegendField{const char* name;const wchar_t* member;bool integer;bool sub;};
const LegendField LEG_LINE[]={{"linetype",L"ltp",true,false},{"aux_linetype",L"fltp",true,false},{"color",L"lclr",true,false},{"width",L"lw",false,false},{"x_coef",L"xscale",false,false},{"y_coef",L"yscale",false,false},{"aux_color",L"fclr",true,false},{"layer",L"layer",true,false}};
const LegendField LEG_POINT[]={{"symbol_no",L"subno",true,true},{"height",L"height",false,true},{"width",L"width",false,true},{"angle",L"angle",false,true},{"pen_width",L"penw",false,true},{"color",L"iclr",true,false},{"layer",L"layer",true,false}};
const LegendField LEG_REGION[]={{"fill_color",L"clr",true,false},{"pattern",L"patno",true,false},{"pattern_height",L"pathei",false,false},{"pattern_width",L"patwid",false,false},{"pattern_color",L"patclr",true,false},{"layer",L"layer",true,false}};
const LegendField* legend_fields(int kind,size_t& size){
 if(kind==0){size=_countof(LEG_LINE);return LEG_LINE;}if(kind==1){size=_countof(LEG_POINT);return LEG_POINT;}
 if(kind==2){size=_countof(LEG_REGION);return LEG_REGION;}throw std::runtime_error("legend item kind");
}
std::string legend_unhex(std::string value,size_t maximum){
 if(value=="-")return "";if(value.size()%2||value.size()>maximum*2)throw std::runtime_error("legend encoded string length");
 std::string result;auto digit=[](char c)->int{if(c>='0'&&c<='9')return c-'0';if(c>='a'&&c<='f')return c-'a'+10;throw std::runtime_error("legend encoded string digit");};
 for(size_t i=0;i<value.size();i+=2){char c=char(digit(value[i])*16+digit(value[i+1]));if(!c)throw std::runtime_error("legend string null");result+=c;}return result;
}
void legend_hex(const char* value,size_t limit){
 size_t n=strnlen_s(value,limit);if(n==limit)throw std::runtime_error("legend unterminated native string");
 for(size_t i=0;i<n;++i)printf("%02x",static_cast<unsigned char>(value[i]));
}
ComDispatch legend_object(const _variant_t& value){
 if(value.vt!=VT_DISPATCH||!value.pdispVal)throw std::runtime_error("legend SDK object");return ComDispatch(value.pdispVal);
}
struct NativeLegend{
 HMODULE library=nullptr;ComDispatch object;void* native=nullptr;
 NativeLegend(ComContext& context,HMODULE module):library(module){
  object=context.create(L"MapGisMapCom.dll",L"{E823F1D0-8E0B-420B-B175-03A6D10D97FE}");
  native=*reinterpret_cast<void**>(reinterpret_cast<BYTE*>(object.GetInterfacePtr())+40);
  if(!native)throw std::runtime_error("legend native object");using Flag=short(__stdcall*)(void*,short);sym<Flag>(library,"_LEGSetDispMsgFlag")(native,0);
 }
 void close(){if(native){using Flag=short(__stdcall*)(void*,short);sym<Flag>(library,"_LEGSetChangeFlag")(native,0);com_invoke(object,L"CloseLegendFile",DISPATCH_METHOD);native=nullptr;}}
 ~NativeLegend(){try{close();}catch(...){}}
};

ComDispatch legend_style(ComContext& context,int kind){
 const wchar_t* classes[]={L"{72FC1102-D61F-40BD-9332-39D670420F02}",L"{01D55128-6611-49AB-972D-0B0E7574E4BC}",L"{8865D679-20FD-4BB9-ADBC-79D2E7509D51}"};
 if(kind<0||kind>2)throw std::runtime_error("legend style kind");return context.create(L"MapGisBasCom1.DLL",classes[kind]);
}

void legend_items(ComContext& context,NativeLegend& legend){
 using Count=int(__stdcall*)(void*,short);using Type=short(__stdcall*)(void*,int);using Get=short(__stdcall*)(void*,int,void*);
 int n=sym<Count>(legend.library,"_LEGGetLogCount")(legend.native,-1);if(n<0||n>1000)throw std::runtime_error("legend count bounds");
 for(int i=0;i<n;++i){
  BYTE header[325]{};require(sym<Get>(legend.library,"_LEGGetCLNLegItem")(legend.native,i,header),"legend item header");
  int kind=sym<Type>(legend.library,"_LEGGetCLNType")(legend.native,i);long code=0;memcpy(&code,header+258,4);
  if(kind<0||kind>2)throw std::runtime_error("legend unsupported saved item");
  BYTE nativeStyle[94]{};const char* getters[]={"_LEGGetCLNLinInfo","_LEGGetCLNPntInfo","_LEGGetCLNRegInfo"};
  require(sym<Get>(legend.library,getters[kind])(legend.native,i,nativeStyle),"legend native style read");
  auto style=legend_style(context,kind);using Convert=short(__stdcall*)(void*,IDispatch*);
  const char* converters[]={"CvtLIN_INFO2ILin_Info","CvtPNT_INFO2IPnt_Info","CvtREG_INFO2IReg_Info"};
  require(sym<Convert>(GetModuleHandleW(L"MapGisBasCom1.DLL"),converters[kind])(nativeStyle,style),"legend style read conversion");ComDispatch sub;
  if(kind==1)sub=legend_object(com_invoke(style,L"sub",DISPATCH_PROPERTYGET));
  printf("{\"event\":\"legend_item\",\"index\":%d,\"type\":%d,\"code\":%ld,\"name_gbk_hex\":\"",i,kind,code);legend_hex(reinterpret_cast<char*>(header),128);
  printf("\",\"description_gbk_hex\":\"");legend_hex(reinterpret_cast<char*>(header)+128,128);printf("\",\"style\":{");
  size_t count=0;auto fields=legend_fields(kind,count);
  for(size_t f=0;f<count;++f){auto value=com_invoke(fields[f].sub?sub.GetInterfacePtr():style.GetInterfacePtr(),fields[f].member,DISPATCH_PROPERTYGET);
   double number=fields[f].integer?double(com_long(value)):(value.vt==VT_R8?value.dblVal:std::numeric_limits<double>::quiet_NaN());
   if(!std::isfinite(number))throw std::runtime_error("legend style scalar");printf("%s\"%s\":%.17g",f?",":"",fields[f].name,number);
  }
  puts("}}");fflush(stdout);
 }
 printf("{\"event\":\"legend_count\",\"count\":%d}\n",n);fflush(stdout);
}

void com_legend(wchar_t** argv,bool creating){
 ComContext context(argv[2],argv[3]);std::wstring program(argv[2]);program=program.substr(0,program.find_last_of(L"/\\"));
 auto path=program+L"/mapleg60.dll";if(digest(path.c_str())!="97ef83a6646cbeb041a3cad65696e29c0da9b6eb7053d8b9a6387be766a7c5c8")throw std::runtime_error("legend SDK build profile");
 HMODULE library=LoadLibraryW(path.c_str());if(!library)throw std::runtime_error("legend native initialization");
 std::wstring file(creating?argv[6]:argv[5]);auto split=file.find_last_of(L"/\\");
 if(split==std::wstring::npos||!SetCurrentDirectoryW(file.substr(0,split).c_str()))throw std::runtime_error("legend owned directory");
 if(!creating){
  if(GetFileAttributesW(file.c_str())==INVALID_FILE_ATTRIBUTES)throw std::runtime_error("legend missing input");
  NativeLegend object(context,library);com_invoke(object.object,L"LoadLegendFile",DISPATCH_METHOD,{_variant_t(file.c_str())});legend_items(context,object);object.close();
  printf("{\"event\":\"legend_inspected\",\"sha256\":\"%s\"}\n",digest(file.c_str()).c_str());puts("{\"event\":\"complete\"}");fflush(stdout);return;
 }
 if(GetFileAttributesW(file.c_str())!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("legend output exists");
 auto control=ansi(argv[7]);std::ifstream input(control.data());int n=0,columns=0;double bounds[4]{};std::string titleHex;
 if(!(input>>n>>bounds[0]>>bounds[1]>>bounds[2]>>bounds[3]>>columns>>titleHex)||n<1||n>1000||columns<1||columns>20||bounds[0]>=bounds[2]||bounds[1]>=bounds[3])throw std::runtime_error("legend control header");
 for(double value:bounds)if(!std::isfinite(value))throw std::runtime_error("legend finite rectangle");auto title=legend_unhex(titleHex,127);if(title.empty())throw std::runtime_error("legend title required");
 {
  NativeLegend object(context,library);
  for(int i=0;i<n;++i){
   int kind=-1,m=0;long code=0;std::string nameHex,descriptionHex;
   if(!(input>>kind>>code>>nameHex>>descriptionHex>>m)||m<1||m>20)throw std::runtime_error("legend item control");
   auto name=legend_unhex(nameHex,90),description=legend_unhex(descriptionHex,127);if(name.empty())throw std::runtime_error("legend name required");
   BYTE header[325]{};memcpy(header,name.data(),name.size());memcpy(header+128,description.data(),description.size());memcpy(header+258,&code,4);
   size_t count=0;auto fields=legend_fields(kind,count);auto style=legend_style(context,kind);ComDispatch sub;
   if(kind==1){com_invoke(style,L"type",DISPATCH_PROPERTYPUT,{_variant_t(long(1))});sub=legend_object(com_invoke(style,L"sub",DISPATCH_PROPERTYGET));}
   std::set<std::string> used;
   for(int f=0;f<m;++f){std::string field;double number=0;if(!(input>>field>>number)||!std::isfinite(number)||!used.insert(field).second)throw std::runtime_error("legend style input");
    size_t at=0;while(at<count&&field!=fields[at].name)++at;if(at==count)throw std::runtime_error("legend unknown style input");
    auto value=fields[at].integer?_variant_t(long(number)):_variant_t(number);if(fields[at].integer&&number!=std::floor(number))throw std::runtime_error("legend style integer");
    com_invoke(fields[at].sub?sub.GetInterfacePtr():style.GetInterfacePtr(),fields[at].member,DISPATCH_PROPERTYPUT,{value});
   }
   if(kind==1)com_invoke(style,L"sub",DISPATCH_PROPERTYPUT,{_variant_t(sub.GetInterfacePtr())});
   using Convert=short(__stdcall*)(IDispatch*,void**);void* nativeStyle=nullptr;
   const char* converters[]={"CvtILin_Info2LIN_INFO","CvtIPnt_Info2PNT_INFO","CvtIReg_Info2REG_INFO"};
   require(sym<Convert>(GetModuleHandleW(L"MapGisBasCom1.DLL"),converters[kind])(style,&nativeStyle),"legend style conversion");if(!nativeStyle)throw std::runtime_error("legend style missing");
   using Append=short(__stdcall*)(void*,void*,void*,void*,char*,long);const char* append[]={"_LEGAppendCLNLIN","_LEGAppendCLNPNT","_LEGAppendCLNREG"};
   require(sym<Append>(library,append[kind])(object.native,header,nativeStyle,nullptr,nullptr,0),"legend append");
  }
  std::string trailing;if(input>>trailing)throw std::runtime_error("legend trailing control data");
  auto name=ansi(file.c_str());using Name=short(__stdcall*)(void*,char*);using Save=short(__stdcall*)(void*);
  sym<Name>(library,"_LEGSetName")(object.native,name.data());require(sym<Save>(library,"_LEGSaveLegend")(object.native),"legend save");
 }
 // A normal fresh COM load refreshes the adapter's rendering cache. A nonempty
 // legitimate title also allocates WT for names on line/region-only legends.
 NativeLegend reopened(context,library);com_invoke(reopened.object,L"LoadLegendFile",DISPATCH_METHOD,{_variant_t(file.c_str())});
 BYTE* layout=*reinterpret_cast<BYTE**>(reinterpret_cast<BYTE*>(reopened.object.GetInterfacePtr())+58);if(!layout)throw std::runtime_error("legend layout object");
 strcpy_s(reinterpret_cast<char*>(layout)+33,128,title.c_str());short cols=short(columns),order=0;memcpy(layout+297,&cols,2);memcpy(layout+295,&order,2);
 auto rect=context.create(L"MapGisBasCom1.DLL",L"{AFF21083-4908-47AD-A892-D1BC938390EF}");const wchar_t* coordinates[]={L"xmin",L"ymin",L"xmax",L"ymax"};
 for(int i=0;i<4;++i)com_invoke(rect,coordinates[i],DISPATCH_PROPERTYPUT,{_variant_t(bounds[i])});com_invoke(reopened.object,L"DispRect",DISPATCH_PROPERTYPUT,{_variant_t(rect.GetInterfacePtr())});
 auto stem=file.substr(0,file.find_last_of(L'.'));std::wstring wt=stem+L".wt",wl=stem+L".wl",wp=stem+L".wp";
 for(auto& output:{wt,wl,wp})if(GetFileAttributesW(output.c_str())!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("legend vector output exists");
 com_invoke(reopened.object,L"WriteLegItemsToFile",DISPATCH_METHOD,{_variant_t(wt.c_str()),_variant_t(wl.c_str()),_variant_t(wp.c_str())});legend_items(context,reopened);reopened.close();
 printf("{\"event\":\"legend_saved\",\"sha256\":\"%s\"}\n",digest(file.c_str()).c_str());puts("{\"event\":\"complete\"}");fflush(stdout);
}
