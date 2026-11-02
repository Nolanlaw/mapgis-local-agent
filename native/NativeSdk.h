// Typed Automation calls in the existing private x86 host. No code evaluation,
// vtable offsets, editor injection, registration or desktop input.
#pragma once
#include <map>
#include <climits>

namespace native_sdk {
using Values=std::map<std::string,_variant_t>;
std::vector<std::string> columns(const std::string& line){
 std::vector<std::string> out;size_t start=0,end;
 while((end=line.find('\t',start))!=std::string::npos){out.push_back(line.substr(start,end-start));start=end+1;}
 out.push_back(line.substr(start));return out;
}
std::string token(const std::string& s){
 if(s.empty()||s.size()>80||s.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")!=std::string::npos)throw std::runtime_error("SDK identifier");return s;
}
std::wstring wide(const std::string& hex){
 if(hex=="-")return L"";if(hex.size()%4||hex.size()>1048576)throw std::runtime_error("SDK UTF16 size");
 std::wstring out;out.reserve(hex.size()/4);
 auto nib=[](char c){if(c>='0'&&c<='9')return c-'0';if(c>='a'&&c<='f')return c-'a'+10;throw std::runtime_error("SDK hex");};
 for(size_t i=0;i<hex.size();i+=4)out.push_back(wchar_t(nib(hex[i])*16+nib(hex[i+1])+(nib(hex[i+2])*16+nib(hex[i+3]))*256));return out;
}
long integer(const std::string& s){size_t end=0;long v=std::stol(s,&end);if(end!=s.size())throw std::runtime_error("SDK integer");return v;}
_variant_t literal(int vt,const std::string& s){
 _variant_t v;
 switch(vt){
 case VT_EMPTY:case VT_NULL:if(s!="-")throw std::runtime_error("SDK null literal");break;
 case VT_I2:{long n=integer(s);if(n<-32768||n>32767)throw std::runtime_error("SDK int16");v.iVal=short(n);break;}
 case VT_I4:v.lVal=integer(s);break;
 case VT_R4:case VT_R8:case VT_DATE:{size_t end=0;double n=std::stod(s,&end);if(end!=s.size()||!std::isfinite(n))throw std::runtime_error("SDK finite real");if(vt==VT_R4){v.fltVal=float(n);if(!std::isfinite(v.fltVal))throw std::runtime_error("SDK float32 overflow");}else v.dblVal=n;break;}
 case VT_BOOL:if(s!="0"&&s!="1")throw std::runtime_error("SDK boolean");v.boolVal=s=="1"?VARIANT_TRUE:VARIANT_FALSE;break;
 case VT_BSTR:{auto text=wide(s);v.bstrVal=SysAllocStringLen(text.data(),UINT(text.size()));if(!v.bstrVal)throw std::runtime_error("SDK string allocation");break;}
 default:throw std::runtime_error("SDK unsupported scalar type");
 }v.vt=static_cast<VARTYPE>(vt);return v;
}
void value_event(const std::string& id,const _variant_t& v){
 token(id);printf("{\"event\":\"sdk_value\",\"id\":\"%s\",\"vt\":%u",id.c_str(),unsigned(v.vt));
 switch(v.vt){
 case VT_EMPTY:case VT_NULL:printf(",\"value\":null");break;
 case VT_I2:printf(",\"value\":%d",int(v.iVal));break;
 case VT_I4:printf(",\"value\":%ld",v.lVal);break;
 case VT_BOOL:printf(",\"value\":%s",v.boolVal?"true":"false");break;
 case VT_R4:case VT_R8:case VT_DATE:{double n=v.vt==VT_R4?double(v.fltVal):v.dblVal;if(!std::isfinite(n))throw std::runtime_error("SDK nonfinite result");printf(",\"value\":%.17g",n);break;}
 case VT_BSTR:{UINT n=SysStringLen(v.bstrVal);if(n>262144)throw std::runtime_error("SDK result string limit");printf(",\"utf16\":\"");for(UINT i=0;i<n;++i){unsigned n=v.bstrVal[i];printf("%02x%02x",n&255,n>>8);}printf("\"");break;}
 case VT_DISPATCH:if(!v.pdispVal)throw std::runtime_error("SDK null object result");printf(",\"object\":true");break;
 default:throw std::runtime_error("SDK unsupported result type");
 }puts("}");fflush(stdout);
}
void expect(const _variant_t& v,const _variant_t& wanted){
 if(v.vt!=wanted.vt)throw std::runtime_error("SDK assertion type");bool ok=false;
 switch(v.vt){
 case VT_EMPTY:case VT_NULL:ok=true;break;case VT_I2:ok=v.iVal==wanted.iVal;break;case VT_I4:ok=v.lVal==wanted.lVal;break;
 case VT_R4:ok=v.fltVal==wanted.fltVal;break;case VT_R8:case VT_DATE:ok=v.dblVal==wanted.dblVal;break;case VT_BOOL:ok=v.boolVal==wanted.boolVal;break;
 case VT_BSTR:ok=SysStringLen(v.bstrVal)==SysStringLen(wanted.bstrVal)&&!memcmp(v.bstrVal,wanted.bstrVal,SysStringByteLen(v.bstrVal));break;
 }if(!ok)throw std::runtime_error("SDK assertion failed; following calls were not executed");
}
void out_argument(int vt,_variant_t& backing,VARIANTARG& argument){
 backing.Clear();ZeroMemory(static_cast<VARIANT*>(&backing),sizeof(VARIANT));backing.vt=VARTYPE(vt);argument.vt=VARTYPE(vt|VT_BYREF);
 switch(vt){
 case VT_I2:argument.piVal=&backing.iVal;break;case VT_I4:argument.plVal=&backing.lVal;break;
 case VT_R4:argument.pfltVal=&backing.fltVal;break;case VT_R8:case VT_DATE:argument.pdblVal=&backing.dblVal;break;
 case VT_BSTR:argument.pbstrVal=&backing.bstrVal;break;case VT_DISPATCH:argument.ppdispVal=&backing.pdispVal;break;
 case VT_BOOL:argument.pboolVal=&backing.boolVal;break;case VT_VARIANT:backing.vt=VT_EMPTY;argument.pvarVal=&backing;break;
 default:backing.vt=VT_EMPTY;throw std::runtime_error("SDK unsupported out type");
 }
}
int run(wchar_t** argv){
 SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
 try{
  DWORD dep=0;BOOL permanent=FALSE;if(!GetProcessDEPPolicy(GetCurrentProcess(),&dep,&permanent)||dep)throw std::runtime_error("SDK legacy host opt-in");
  printf("{\"event\":\"host\",\"protocol\":1,\"pid\":%lu,\"bits\":32,\"dep_flags\":%lu,\"desktop\":\"%s\"}\n",GetCurrentProcessId(),dep,desktop_name().c_str());fflush(stdout);
  ComContext context(argv[2],argv[3]);Values values;std::ifstream input(argv[4]);std::string line;
  if(!std::getline(input,line)||line!="SDK1")throw std::runtime_error("SDK control version");int count=0;
  while(std::getline(input,line)){
   if(++count>4096)throw std::runtime_error("SDK call limit");auto c=columns(line);
   if(c.size()==4&&c[0]=="create"){
    auto id=token(c[1]);if(values.count(id))throw std::runtime_error("SDK duplicate identity");
    auto library=wide(c[2]),cls=wide(c[3]);
    if(library!=L"MapGisBasCom1.DLL")throw std::runtime_error("SDK library policy");
    auto object=context.create(library.c_str(),cls.c_str());
    if(cls==L"{815B7C63-500E-4D86-BFFD-35677B9CEC53}"||cls==L"{C18776C2-9AF5-4A6F-88C9-7F96F2E6DDF8}"||cls==L"{6F5430C6-5A6D-4D4C-88C4-CAFFAFF66AF1}"){
     com_invoke(object,L"ShowErrMsg",DISPATCH_PROPERTYPUT,{_variant_t(false)});com_invoke(object,L"Warning",DISPATCH_PROPERTYPUT,{_variant_t(false)});
    }
    values.emplace(id,_variant_t(object.GetInterfacePtr(),true));value_event(id,values.at(id));
   }else if(c.size()==8&&c[0]=="invoke"){
    auto id=token(c[1]);if(values.count(id))throw std::runtime_error("SDK duplicate result");
    const auto& target=values.at(token(c[2]));if(target.vt!=VT_DISPATCH||!target.pdispVal)throw std::runtime_error("SDK target object");
    auto member=wide(c[3]);LPOLESTR name=const_cast<LPOLESTR>(member.c_str());DISPID dispid=0;
    com_require(target.pdispVal->GetIDsOfNames(IID_NULL,&name,1,LOCALE_USER_DEFAULT,&dispid),"SDK member lookup");
    if(dispid!=integer(c[4]))throw std::runtime_error("SDK contract DISPID changed");long flags=integer(c[5]),n=integer(c[6]);
    if((flags!=1&&flags!=2&&flags!=4&&flags!=8)||n<0||n>64||c[7]!="-")throw std::runtime_error("SDK invoke control");
    std::vector<_variant_t> backing(n);std::vector<VARIANTARG> args(n);std::vector<std::string> outs(n);
    for(long i=0;i<n;++i){
     if(!std::getline(input,line))throw std::runtime_error("SDK argument missing");auto a=columns(line);VariantInit(&args[i]);
     if(a.size()==3&&a[0]=="value"){backing[i]=literal(integer(a[1]),a[2]);args[i]=static_cast<const VARIANT&>(backing[i]);}
     else if(a.size()==2&&a[0]=="ref"){backing[i]=values.at(token(a[1]));args[i]=static_cast<const VARIANT&>(backing[i]);}
     else if(a.size()==3&&a[0]=="out"){outs[i]=token(a[2]);if(values.count(outs[i]))throw std::runtime_error("SDK duplicate out identity");out_argument(integer(a[1]),backing[i],args[i]);}
     else throw std::runtime_error("SDK argument syntax");
    }
    std::reverse(args.begin(),args.end());DISPID put=DISPID_PROPERTYPUT;DISPPARAMS params{};params.cArgs=UINT(n);params.rgvarg=args.data();
    if(flags&12){params.cNamedArgs=1;params.rgdispidNamedArgs=&put;}
    _variant_t result;EXCEPINFO info{};UINT arg=UINT_MAX;
    HRESULT hr=target.pdispVal->Invoke(dispid,IID_NULL,LOCALE_USER_DEFAULT,WORD(flags),&params,&result,&info,&arg);
    if(info.pfnDeferredFillIn)info.pfnDeferredFillIn(&info);
    if(FAILED(hr))fprintf(stderr,"SDK step %s, HRESULT 0x%08lx, reversed argument %u, exception 0x%08lx\n",id.c_str(),hr,arg,info.scode);
    if(info.bstrSource)SysFreeString(info.bstrSource);if(info.bstrDescription)SysFreeString(info.bstrDescription);if(info.bstrHelpFile)SysFreeString(info.bstrHelpFile);
    com_require(hr,"SDK invoke");values.emplace(id,result);value_event(id,result);
    for(long i=0;i<n;++i)if(!outs[i].empty()){if(!values.emplace(outs[i],backing[i]).second)throw std::runtime_error("SDK duplicate output");value_event(outs[i],backing[i]);}
   }else if(c.size()==4&&c[0]=="expect")expect(values.at(token(c[1])),literal(integer(c[2]),c[3]));
   else throw std::runtime_error("SDK control syntax");
  }
  if(!input.eof())throw std::runtime_error("SDK control read failure");values.clear();puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const _com_error& e){fprintf(stderr,"SDK Automation error 0x%08lx\n",e.Error());return 1;}
 catch(const std::exception& e){fprintf(stderr,"%s\n",e.what());return 1;}
}
}
