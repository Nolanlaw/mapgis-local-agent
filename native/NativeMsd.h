// Exact CMsiDB public cdecl wrappers. Only owned index copies are opened:
// msiCloseMsiDB can autosave the index, including after an inspection.
#pragma once
bool msd_leaf(const std::string& name){
 if(name.empty()||name.size()>60||name=="."||name=="..")return false;
 for(unsigned char c:name)if(!((c>='a'&&c<='z')||(c>='A'&&c<='Z')||(c>='0'&&c<='9')||c=='-'||c=='_'||c=='.'))return false;
 return name.size()>4&&_stricmp(name.c_str()+name.size()-4,".msi")==0;
}

int msd_main(int argc,wchar_t**argv){
 SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
 using Create=void*(__cdecl*)(char*);using Open=void*(__cdecl*)(char*,char*,char*,char*);
 using Close=bool(__cdecl*)(void*);using Add=bool(__cdecl*)(void*,char*);using Save=bool(__cdecl*)(void*,char*);
 using Remove=bool(__cdecl*)(void*,int);using Count=int(__cdecl*)(void*);using Name=bool(__cdecl*)(void*,int,char*);
 using Range=bool(__cdecl*)(void*,double*,double*,double*,double*);
 void* database=nullptr;Close close=nullptr;
 try{
  if(digest(argv[2])!=BAS_SHA||digest(argv[3])!="e3696fdd50b37ab853f4c6701df1c46468e13e09230adadc82d2096e22d98235")throw std::runtime_error("MSD native build profile");
  std::wstring dll(argv[2]);SetDllDirectoryW(dll.substr(0,dll.find_last_of(L"/\\")).c_str());
  std::wstring imagePath=dll.substr(0,dll.find_last_of(L"/\\"))+L"/msibase.dll";
  if(digest(imagePath.c_str())!="1a2098e4f0086d4f7fec7de2c6b1f7c3705b31ab2bf216ced9bb9a21647e7245")throw std::runtime_error("MSD image dependency build");
  if(!LoadLibraryW(argv[2]))throw std::runtime_error("MSD BAS initialization");
  HMODULE library=LoadLibraryW(argv[3]),image=GetModuleHandleW(L"MsiBase.dll");
  if(!library||!image)throw std::runtime_error("MSD library initialization");
  DWORD dep=0;BOOL permanent=FALSE;if(!GetProcessDEPPolicy(GetCurrentProcess(),&dep,&permanent)||dep)throw std::runtime_error("MSD owned legacy compatibility");
  printf("{\"event\":\"host\",\"protocol\":1,\"pid\":%lu,\"bits\":32,\"dep_flags\":%lu,\"desktop\":\"%s\"}\n",GetCurrentProcessId(),dep,desktop_name().c_str());fflush(stdout);
  bool creating=wcscmp(argv[4],L"create")==0,editing=wcscmp(argv[4],L"edit")==0,inspecting=wcscmp(argv[4],L"inspect")==0;
  if((!creating&&!editing&&!inspecting)||(inspecting?argc!=7:argc!=8))throw std::runtime_error("MSD operation arguments");
  auto output=ansi(argv[6]);
  if(!inspecting&&GetFileAttributesW(argv[6])!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("MSD output exists");
  close=sym<Close>(library,MAKEINTRESOURCEA(4));
  if(creating)database=sym<Create>(library,MAKEINTRESOURCEA(6))(output.data());
  else{
   // Preserve even a direct CLI caller's input index. All member references
   // have already been reduced to bounded same-folder names by the parent.
   wchar_t snapshot[80];swprintf_s(snapshot,L"msd-read-%lu.msd",GetCurrentProcessId());
   if(!CopyFileW(argv[5],snapshot,TRUE))throw std::runtime_error("MSD private index snapshot");
   auto input=ansi(snapshot);database=sym<Open>(library,MAKEINTRESOURCEA(20))(input.data(),nullptr,nullptr,nullptr);
  }
  if(!database)throw std::runtime_error("MSD native open/create");
  auto count=sym<Count>(library,MAKEINTRESOURCEA(16));auto name=sym<Name>(library,MAKEINTRESOURCEA(15));
  if(!inspecting){
   auto control=ansi(argv[7]);std::ifstream stream(control.data());int addCount=0,removeCount=0;
   if(!(stream>>addCount>>removeCount)||addCount<0||addCount>1000||removeCount<0||removeCount>1000)throw std::runtime_error("MSD change counts");
   int original=count(database);std::vector<int> removing;std::set<int> unique;
   for(int i=0,index;i<removeCount;++i){if(!(stream>>index)||index<0||index>=original||!unique.insert(index).second)throw std::runtime_error("MSD member index");removing.push_back(index);}
   std::sort(removing.rbegin(),removing.rend());
   for(int index:removing)if(!sym<Remove>(library,MAKEINTRESOURCEA(8))(database,index))throw std::runtime_error("MSD native member removal");
   using ImageOpen=int(__cdecl*)(char*,void**);using ImageClose=int(__cdecl*)(void*);using GCPs=int(__cdecl*)(void*,unsigned*);
   for(int i=0;i<addCount;++i){
    std::string member;if(!(stream>>member)||!msd_leaf(member))throw std::runtime_error("MSD bounded relative MSI name");
    void* handle=nullptr;if(sym<ImageOpen>(image,"msOpenMsiFileForRead")(&member[0],&handle)||!handle)throw std::runtime_error("MSD input image open");
    unsigned controls=0;int result=sym<GCPs>(image,"msGetGCPNumsOfMSI")(handle,&controls);int ended=sym<ImageClose>(image,"msCloseMsiFileForRead")(handle);
    if(result||ended||controls<3)throw std::runtime_error("MSD needs at least three native image GCPs; assign coordinates before adding");
    if(!sym<Add>(library,MAKEINTRESOURCEA(1))(database,&member[0]))throw std::runtime_error("MSD native member insertion");
   }
   std::string trailing;if(stream>>trailing)throw std::runtime_error("MSD trailing changes");
   if(!sym<Save>(library,MAKEINTRESOURCEA(23))(database,output.data()))throw std::runtime_error("MSD native save");
  }
  int n=count(database);if(n<0||n>1000)throw std::runtime_error("MSD member limit");
  for(int i=0;i<n;++i){char member[512]{};if(!name(database,i,member)||!msd_leaf(member))throw std::runtime_error("MSD native name readback");
   using ImageOpen=int(__cdecl*)(char*,void**);using ImageClose=int(__cdecl*)(void*);
   using ImageRange=int(__cdecl*)(void*,int,double*,double*,double*,double*);
   void* handle=nullptr;if(sym<ImageOpen>(image,"msOpenMsiFileForRead")(member,&handle)||!handle)throw std::runtime_error("MSD member readback open");
   double x0=0,x1=0,y0=0,y1=0;
   int result=sym<ImageRange>(image,"msGetUserCoordinateRangle")(handle,1,&x0,&x1,&y0,&y1);
   int ended=sym<ImageClose>(image,"msCloseMsiFileForRead")(handle);
   if(result||ended||!std::isfinite(x0)||!std::isfinite(x1)||!std::isfinite(y0)||!std::isfinite(y1)||x0>x1||y0>y1)throw std::runtime_error("MSD member native extent");
   printf("{\"event\":\"msd_member\",\"index\":%d,\"name\":\"%s\",\"native_bounds\":[%.17g,%.17g,%.17g,%.17g]}\n",i+1,member,x0,y0,x1,y1);}
  double xmin=0,xmax=0,ymin=0,ymax=0;
  if(n){
   if(!sym<Range>(library,MAKEINTRESOURCEA(13))(database,&xmin,&xmax,&ymin,&ymax)||!std::isfinite(xmin)||!std::isfinite(xmax)||!std::isfinite(ymin)||!std::isfinite(ymax)||xmin>xmax||ymin>ymax)throw std::runtime_error("MSD native extent");
   printf("{\"event\":\"msd\",\"members\":%d,\"native_bounds\":[%.17g,%.17g,%.17g,%.17g]}\n",n,xmin,ymin,xmax,ymax);
  }else puts("{\"event\":\"msd\",\"members\":0,\"native_bounds\":null}");
  if(!close(database))throw std::runtime_error("MSD native close");database=nullptr;
  if(!inspecting)printf("{\"event\":\"msd_saved\",\"sha256\":\"%s\"}\n",digest(argv[6]).c_str());
  puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const std::exception&e){if(database&&close)close(database);fprintf(stderr,"%s\n",e.what());return 1;}
}
