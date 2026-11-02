// Own raw XYZ line area; original BAS60 records and original Tin3DView draw.
#pragma once
struct TinLines {
 Session source;short area=0;void* view=nullptr;
 ~TinLines(){
  if(view){auto data=state(view);data[201]=0;short empty=0;memcpy(data+202,&empty,2);}
  if(area){::Area wrapper{};wrapper.area=area;normalize_empty_attributes(source.library,wrapper);using Dirty=short(__stdcall*)(int,int);using Drop=short(__stdcall*)(int);sym<Dirty>(source.library,MAKEINTRESOURCEA(88))(area,0);sym<Drop>(source.library,MAKEINTRESOURCEA(4))(area);}
 }
 void bind(void* owned_view,const std::wstring& folder,const double* bounds){
  auto name=ansi((folder+L"/lines.xyz").c_str());if(GetFileAttributesA(name.data())==INVALID_FILE_ATTRIBUTES)return;
  source.library=GetModuleHandleW(L"bas60.dll");check(source.library!=nullptr,"TIN line BAS");load_area(source,0,folder+L"/overlay.wl");
  long active=0,upper=0;require(sym<Count>(source.library,"?_GetLinNum@C60_WorkArea@@QAEFPAJ0@Z")(&source.area,&active,&upper),"TIN source WL count");check(active>=1&&active<=20000&&upper>active,"TIN source WL profile");
  using Kind=short(__stdcall*)(int);short kind=sym<Kind>(source.library,MAKEINTRESOURCEA(165))(source.area.area);check(kind>0,"TIN source line native structure");area=sym<Kind>(source.library,MAKEINTRESOURCEA(18))(kind);check(area>0,"owned native XYZ area");
  std::ifstream input(name.data());input.imbue(std::locale::classic());std::string magic,extra;long records=0;input>>magic>>records;check(input&&magic=="MGTINWL1"&&records==active,"TIN XYZ record count");
  using Get=short(__stdcall*)(int,long,void**,long*,void*,short*);using Append=long(__stdcall*)(int,const double*,long,void*);using Read=short(__stdcall*)(int,long,void**,long*,void*);
  size_t total=0;std::set<long> seen;
  for(long i=1;i<=records;++i){
   long id=0,n=0;input>>id>>n;check(input&&id>=1&&id<upper&&seen.insert(id).second&&n>=2&&n<=20000,"TIN XYZ physical ID/vertices");total+=n;check(total<=200000,"TIN XYZ total bound");
   unsigned char parameters[68]{},actual_parameters[68]{};void* original=nullptr;long original_n=0;short dimension=2;
   require(sym<Get>(source.library,MAKEINTRESOURCEA(1551))(source.area.area,id,&original,&original_n,parameters,&dimension),"source line parameters");check(original&&original_n>=2&&dimension==2,"source line XY profile");
   std::vector<double> xyz(size_t(n)*3);for(double& value:xyz){input>>value;check(input&&std::isfinite(value)&&fabs(value)<3.4e18,"TIN XYZ finite values");}
   for(long j=0;j<n;++j)check(xyz[3*j]>=-1e-8&&xyz[3*j]<=bounds[2]-bounds[0]+1e-8&&xyz[3*j+1]>=-1e-8&&xyz[3*j+1]<=bounds[3]-bounds[1]+1e-8,"TIN local XYZ bounds");
   check(sym<Append>(source.library,MAKEINTRESOURCEA(1555))(area,xyz.data(),n,parameters)==i,"original XYZ line append");void* actual=nullptr;long actual_n=0;
   require(sym<Read>(source.library,MAKEINTRESOURCEA(1554))(area,i,&actual,&actual_n,actual_parameters),"original XYZ readback");
   check(actual&&actual_n==n&&!memcmp(actual,xyz.data(),xyz.size()*8)&&!memcmp(parameters,actual_parameters,sizeof(parameters)),"native XYZ and graphic parameters exact");
  }
  check(!(input>>extra),"TIN XYZ trailing input");auto data=state(owned_view);short prior=0;memcpy(&prior,data+202,2);check(!prior&&!data[201],"new native TIN overlay state empty");
  view=owned_view;memcpy(data+202,&area,2);data[201]=1;
  printf("{\"event\":\"tin_lines_bound\",\"active\":%ld,\"record_upper_bound\":%ld,\"vertices\":%u,\"all_xyz_equal\":true,\"graphic_parameters_equal\":true,\"licensed_solver_called\":false}\n",active,upper,unsigned(total));fflush(stdout);
 }
};
