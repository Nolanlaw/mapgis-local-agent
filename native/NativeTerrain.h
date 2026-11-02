// Exact-build DTM exports, recovered from the installed W60_Tin and verified
// against analytic cases. These calls run only in this owned legacy process.
#pragma once
#include <array>
#include "NativeTin.h"
#pragma pack(push,2)
struct TerrainGrid {int nx,ny;double xmin,xmax,ymin,ymax,zmin,zmax;};
struct TerrainSearch {short mode,minimum,per_sector,empty_sectors;double radius;};
struct TerrainIdw {short power;double smoothing,ratio,angle;};
#pragma pack(pop)
static_assert(sizeof(TerrainGrid)==56&&sizeof(TerrainSearch)==16&&sizeof(TerrainIdw)==26,"DTM x86 ABI");

int terrain_main(wchar_t**argv){
 SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
 try{
  std::wstring dll(argv[3]),program=dll.substr(0,dll.find_last_of(L"/\\"));
  if(digest(dll.c_str())!="94e25abdb102fb50d791aa689b114fddd48dfecf9eb24a2a82329ed4ddce0404"
    ||digest((program+L"/bas60.dll").c_str())!=BAS_SHA
    ||digest((program+L"/TinBas60.dll").c_str())!="ac02d4eb00fcf2dfa9b8f64703f08b28af4bfa171bf60d9cb15d93d341df8270")
   throw std::runtime_error("terrain DLL profile mismatch");
  SetDllDirectoryW(program.c_str());HMODULE tin=LoadLibraryW(dll.c_str());if(!tin)throw std::runtime_error("terrain initialization: Windows error "+std::to_string(GetLastError()));
  DWORD dep=0;BOOL permanent=FALSE;if(!GetProcessDEPPolicy(GetCurrentProcess(),&dep,&permanent)||dep)throw std::runtime_error("terrain legacy compatibility unavailable");
  printf("{\"event\":\"host\",\"pid\":%lu,\"bits\":32,\"dep_flags\":%lu,\"desktop\":\"%s\"}\n",GetCurrentProcessId(),dep,desktop_name().c_str());fflush(stdout);
  if(GetFileAttributesW(argv[5])!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("terrain output exists");
  terrain_temp_folder(argv[5]);
  puts("{\"event\":\"terrain_temporary_folder_bound\"}");fflush(stdout);
  auto input=ansi(argv[4]),output=ansi(argv[5]);TerrainGrid grid{};
  if(wcscmp(argv[2],L"tin")==0||wcscmp(argv[2],L"tin.inspect")==0||wcscmp(argv[2],L"tin.write")==0){
   native_tin(tin,argv[4],argv[5],wcscmp(argv[2],L"tin")==0?1:wcscmp(argv[2],L"tin.write")==0?2:0);
  }else if(wcscmp(argv[2],L"idw")==0){
   std::ifstream f(input.data());f.imbue(std::locale::classic());int n=0,power=0;
   TerrainSearch search{1,1,1,0,0};TerrainIdw parameters{};
   if(!(f>>grid.nx>>grid.ny>>grid.xmin>>grid.xmax>>grid.ymin>>grid.ymax>>n
      >>power>>search.radius>>parameters.ratio>>parameters.angle)
      ||n<3||n>1000000||grid.nx<2||grid.ny<2||grid.nx>32767||grid.ny>32767
      ||static_cast<long long>(grid.nx)*grid.ny>2000000||power<1||power>16
      ||!std::isfinite(search.radius)||search.radius<=0||!std::isfinite(parameters.ratio)||parameters.ratio<1
      ||!std::isfinite(parameters.angle)||!std::isfinite(grid.xmin)||!std::isfinite(grid.xmax)
      ||!std::isfinite(grid.ymin)||!std::isfinite(grid.ymax)||grid.xmin>=grid.xmax||grid.ymin>=grid.ymax)
    throw std::runtime_error("terrain IDW parameters");
   parameters.power=short(power);parameters.smoothing=0;
   std::vector<double> x(n),y(n),z(n);
   for(int i=0;i<n;++i)if(!(f>>x[i]>>y[i]>>z[i])||!std::isfinite(x[i])||!std::isfinite(y[i])||!std::isfinite(z[i])||fabs(z[i])>=3.4e18)
    throw std::runtime_error("terrain samples");
   std::string extra;if(f>>extra)throw std::runtime_error("terrain trailing input");
   using Build=short(__stdcall*)(double*,double*,double*,int,TerrainGrid*,TerrainSearch*,TerrainIdw*,char*);
   require(sym<Build>(tin,MAKEINTRESOURCEA(153))(x.data(),y.data(),z.data(),n,&grid,&search,&parameters,output.data()),"native IDW failed");
   printf("{\"event\":\"grid_saved\",\"nx\":%d,\"ny\":%d,\"zmin\":%.17g,\"zmax\":%.17g,\"sha256\":\"%s\"}\n",grid.nx,grid.ny,grid.zmin,grid.zmax,digest(argv[5]).c_str());fflush(stdout);
  }else if(wcscmp(argv[2],L"inspect")==0){
   using Create=void*(__cdecl*)();using Load=short(__stdcall*)(void*,char*);
   using Info=short(__stdcall*)(void*,TerrainGrid*);using Data=double*(__stdcall*)(void*);
   using Dirty=short(__stdcall*)(void*,int);using Drop=short(__stdcall*)(void*);
   void* area=sym<Create>(tin,MAKEINTRESOURCEA(4))();if(!area)throw std::runtime_error("terrain area create");
   require(sym<Load>(tin,MAKEINTRESOURCEA(15))(area,input.data()),"terrain grid load");
   if(sym<Info>(tin,MAKEINTRESOURCEA(59))(area,&grid)!=2||grid.nx<2||grid.ny<2||static_cast<long long>(grid.nx)*grid.ny>2000000)
    throw std::runtime_error("terrain grid profile");
   double* values=sym<Data>(tin,MAKEINTRESOURCEA(67))(area);if(!values)throw std::runtime_error("terrain grid buffer");
   // Physical slot zero is reserved by the native DEM work area.
   std::ofstream f(output.data(),std::ios::binary);f.write(reinterpret_cast<char*>(values+1),size_t(grid.nx)*grid.ny*8);f.close();
   if(!f)throw std::runtime_error("terrain readback stream");
   printf("{\"event\":\"grid\",\"nx\":%d,\"ny\":%d,\"bounds\":[%.17g,%.17g,%.17g,%.17g],\"range\":[%.17g,%.17g]}\n",grid.nx,grid.ny,grid.xmin,grid.ymin,grid.xmax,grid.ymax,grid.zmin,grid.zmax);fflush(stdout);
   // Loading Surfer GRD creates a private native conversion buffer. It is a
   // read operation; discard that buffer through the documented modify flag.
   sym<Dirty>(tin,MAKEINTRESOURCEA(10))(area,0);require(sym<Drop>(tin,MAKEINTRESOURCEA(5))(area),"terrain area close");
  }else throw std::runtime_error("terrain operation");
  puts("{\"event\":\"complete\"}");fflush(stdout);return 0;
 }catch(const std::exception&e){fprintf(stderr,"%s\n",e.what());return 1;}
}
