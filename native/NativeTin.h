// CDT 2.0.1 triangulation plus exact installed W60_Tin record/file APIs.
// MGNT0001 is our readback transport, never a proprietary TIN file encoder.
#pragma once
#define CDT_ENSURE_PRECISE_MATH_IN_CONSTRUCTIONS
#include "../vendor/cdt/CDT/include/CDT.h"
#pragma pack(push,2)
struct NativeTinPoint {double x,y,z;unsigned short reserved;signed char state;unsigned char pad;};
struct NativeTinTriangle {int vertices[3],neighbors[3];signed char state;unsigned char pad;};
#pragma pack(pop)
static_assert(sizeof(NativeTinPoint)==28&&sizeof(NativeTinTriangle)==26,"TIN record ABI");

void terrain_temp_folder(const wchar_t* output){
 std::wstring full(output),folder=full.substr(0,full.find_last_of(L"/\\"));auto path=ansi(folder.c_str());
 if(strlen(path.data())>=128||folder==full)throw std::runtime_error("terrain temporary folder");
 HMODULE bas=GetModuleHandleW(L"bas60.dll"),root=GetModuleHandleW(L"basroot.dll");wchar_t filename[MAX_PATH]{};
 if(!bas||!root||!GetModuleFileNameW(root,filename,MAX_PATH)||digest(filename)!="d1109d09d3708911a68a490fc86d64e8fbf30b3f53a7ffce8de2241ec5cc36ba")throw std::runtime_error("terrain environment build");
 using Query=void*(__stdcall*)(short);using Load=void*(__stdcall*)(HMODULE);
 void* previous=sym<Query>(bas,MAKEINTRESOURCEA(226))(0);if(!previous)throw std::runtime_error("terrain environment read");
 unsigned char environment[640];memcpy(environment,previous,sizeof environment);
 memset(environment+384,0,128);memcpy(environment+384,path.data(),strlen(path.data()));
 void* owned=sym<Load>(root,"rt_LoadEnv")(GetModuleHandleW(nullptr));if(!owned)throw std::runtime_error("terrain owned environment");
 memcpy(owned,environment,sizeof environment);
 void* check=sym<Query>(bas,MAKEINTRESOURCEA(226))(0);
 if(!check||memcmp(static_cast<unsigned char*>(check)+384,environment+384,128))throw std::runtime_error("terrain temporary folder readback");
}

// mode 0 reads a native file; 1 triangulates XYZ; 2 writes validated transport.
void native_tin(HMODULE library,const wchar_t* inputPath,const wchar_t* outputPath,int mode){
 auto input=ansi(inputPath),output=ansi(outputPath);
 // The installed save routine uses 77-byte path buffers. Callers supply an
 // owned short alias; never pass a long path into that legacy routine.
 if(strlen(input.data())>70||strlen(output.data())>70)throw std::runtime_error("TIN short native alias required");
 using Create=void*(__cdecl*)();using Drop=short(__stdcall*)(void*);using Dirty=short(__stdcall*)(void*,int);
 using Load=short(__stdcall*)(void*,char*);using Count=short(__stdcall*)(void*,int*,int*);
 using Read=short(__stdcall*)(void*,int,void*);using Append=int(__stdcall*)(void*,void*);
 void* area=nullptr;
 try{
  std::vector<NativeTinPoint> points;std::vector<NativeTinTriangle> triangles;int n=0,m=0;
  if(mode==2){
   std::ifstream stream(input.data(),std::ios::binary);char magic[8]{};
   stream.read(magic,8);stream.read(reinterpret_cast<char*>(&n),4);stream.read(reinterpret_cast<char*>(&m),4);
   if(!stream||memcmp(magic,"MGNT0001",8)||n<3||n>100000||m<1||m>200000)throw std::runtime_error("TIN transport header");
   points.resize(n+1);triangles.resize(m+1);
   stream.read(reinterpret_cast<char*>(points.data()),points.size()*sizeof(NativeTinPoint));
   stream.read(reinterpret_cast<char*>(triangles.data()),triangles.size()*sizeof(NativeTinTriangle));
   if(!stream||stream.peek()!=std::char_traits<char>::eof())throw std::runtime_error("TIN transport length");
   for(int i=1;i<=n;++i){auto& p=points[i];if(p.state!=1||!std::isfinite(p.x)||!std::isfinite(p.y)||!std::isfinite(p.z)||fabs(p.z)>=3.4e18)throw std::runtime_error("TIN transport point");}
   for(int i=1;i<=m;++i){auto& t=triangles[i];if(t.state!=1)throw std::runtime_error("TIN transport triangle");
    for(int j=0;j<3;++j)if(t.vertices[j]<1||t.vertices[j]>n||t.neighbors[j]<0||t.neighbors[j]>m)throw std::runtime_error("TIN transport indices");}
  }
  if(mode==1){
   std::ifstream stream(input.data());stream.imbue(std::locale::classic());std::string header;std::getline(stream,header);
   std::istringstream firstLine(header);int edgeCount=0;
   if(!(firstLine>>n)||n<3||n>100000)throw std::runtime_error("TIN point count");
   if(firstLine>>edgeCount){std::string extra;if(firstLine>>extra)throw std::runtime_error("TIN input header");}
   if(edgeCount<0||edgeCount>300000)throw std::runtime_error("TIN constraint count");
   std::vector<std::array<double,3>> xyz(n);
   for(auto& p:xyz)if(!(stream>>p[0]>>p[1]>>p[2])||!std::isfinite(p[0])||!std::isfinite(p[1])||!std::isfinite(p[2])||fabs(p[2])>=3.4e18)throw std::runtime_error("TIN points");
   std::vector<int> edges(2*edgeCount);
   for(int i=0;i<edgeCount;++i)if(!(stream>>edges[2*i]>>edges[2*i+1])||edges[2*i]<0||edges[2*i]>=n||edges[2*i+1]<0||edges[2*i+1]>=n||edges[2*i]==edges[2*i+1])throw std::runtime_error("TIN constraint endpoints");
   std::string extra;if(stream>>extra)throw std::runtime_error("TIN trailing input");
   // Upstream CDT stays unmodified under MPL-2.0. It constructs XY topology;
   // every elevation below comes from the original input, with no new nodes.
   std::vector<CDT::V2d<double>> xy;for(auto& p:xyz)xy.emplace_back(p[0],p[1]);
   CDT::Triangulation<double> model(CDT::VertexInsertionOrder::AsProvided);
   model.insertVertices(xy);
   if(edgeCount){std::vector<CDT::Edge> constraints;for(int i=0;i<edgeCount;++i)constraints.emplace_back(CDT::VertInd(edges[2*i]),CDT::VertInd(edges[2*i+1]));model.insertEdges(constraints);}
   model.eraseSuperTriangle();
   m=int(model.triangles.size());
   if(model.vertices.size()!=size_t(n)||m<1||m>2*n)throw std::runtime_error("CDT vertex/face count");
   points.resize(n+1);triangles.resize(m+1);
   for(int i=0;i<n;++i){
    if(model.vertices[i].x!=xyz[i][0]||model.vertices[i].y!=xyz[i][1])throw std::runtime_error("CDT input point identity");
    points[i+1]={xyz[i][0],xyz[i][1],xyz[i][2],0,1,0};
   }
   for(int i=0;i<m;++i){
    auto& source=model.triangles[i];auto& target=triangles[i+1];target.state=1;
    // Both formats number neighbors by edges (v0,v1), (v1,v2), (v2,v0).
    // This order was checked against a real native reference TIN.
    for(int j=0;j<3;++j){target.vertices[j]=int(source.vertices[j])+1;target.neighbors[j]=source.neighbors[j]==CDT::noNeighbor?0:int(source.neighbors[j])+1;}
   }
   printf("{\"event\":\"tin_triangulated\",\"algorithm\":\"CDT 2.0.1\",\"input_points\":%d,\"constraints\":%d}\n",n,edgeCount);fflush(stdout);
  }
  if(mode!=0){
   area=sym<Create>(library,MAKEINTRESOURCEA(4))();if(!area)throw std::runtime_error("TIN work area");
   for(int i=1;i<=n;++i)if(sym<Append>(library,MAKEINTRESOURCEA(20))(area,&points[i])!=i)throw std::runtime_error("native TIN point record");
   for(int i=1;i<=m;++i)if(sym<Append>(library,MAKEINTRESOURCEA(31))(area,&triangles[i])!=i)throw std::runtime_error("native TIN triangle record");
   require(sym<Load>(library,MAKEINTRESOURCEA(16))(area,output.data()),"native TIN save");
   if(GetFileAttributesW(outputPath)==INVALID_FILE_ATTRIBUTES)throw std::runtime_error("native TIN output missing");
   printf("{\"event\":\"tin_saved\",\"points\":%d,\"triangles\":%d,\"sha256\":\"%s\"}\n",n,m,digest(outputPath).c_str());fflush(stdout);
  }else{
   area=sym<Create>(library,MAKEINTRESOURCEA(4))();if(!area)throw std::runtime_error("TIN work area");
   require(sym<Load>(library,MAKEINTRESOURCEA(15))(area,input.data()),"native TIN load");
   int activePoints=0,activeTriangles=0,pointSlots=0,triangleSlots=0;
   require(sym<Count>(library,MAKEINTRESOURCEA(53))(area,&activePoints,&pointSlots),"TIN point counts");
   require(sym<Count>(library,MAKEINTRESOURCEA(54))(area,&activeTriangles,&triangleSlots),"TIN triangle counts");
   if(pointSlots<4||pointSlots>100001||triangleSlots<2||triangleSlots>200001||activePoints!=pointSlots-1||activeTriangles!=triangleSlots-1)throw std::runtime_error("TIN dense active record profile");
   n=pointSlots-1;m=triangleSlots-1;points.resize(pointSlots);triangles.resize(triangleSlots);
   for(int i=1;i<=n;++i)require(sym<Read>(library,MAKEINTRESOURCEA(21))(area,i,&points[i]),"native TIN point read");
   for(int i=1;i<=m;++i)require(sym<Read>(library,MAKEINTRESOURCEA(32))(area,i,&triangles[i]),"native TIN triangle read");
   std::ofstream outputFile(output.data(),std::ios::binary);outputFile.write("MGNT0001",8);
   outputFile.write(reinterpret_cast<char*>(&n),4);outputFile.write(reinterpret_cast<char*>(&m),4);
   outputFile.write(reinterpret_cast<char*>(points.data()),points.size()*sizeof(NativeTinPoint));
   outputFile.write(reinterpret_cast<char*>(triangles.data()),triangles.size()*sizeof(NativeTinTriangle));outputFile.close();
   if(!outputFile)throw std::runtime_error("TIN readback stream");
   printf("{\"event\":\"tin\",\"points\":%d,\"triangles\":%d}\n",n,m);fflush(stdout);
  }
  sym<Dirty>(library,MAKEINTRESOURCEA(10))(area,0);require(sym<Drop>(library,MAKEINTRESOURCEA(5))(area),"TIN work area close");area=nullptr;
 }catch(...){if(area){sym<Dirty>(library,MAKEINTRESOURCEA(10))(area,0);sym<Drop>(library,MAKEINTRESOURCEA(5))(area);}throw;}
}
