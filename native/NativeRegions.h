// WP solid fills through existing unmodified CDT and original height/palette APIs.
// Never calls the licensed original TinBas region triangulator.
#pragma once
struct RegionMesh {
 int id=0;long color=0;COLORREF rgb=0;
 std::vector<std::array<float,3>> vertices;std::vector<std::array<unsigned,3>> triangles;
};
bool open_regions(const std::wstring& folder,Session& session,long& active,long& upper){
 auto file=folder+L"/regions.wp";if(GetFileAttributesW(file.c_str())==INVALID_FILE_ATTRIBUTES)return false;
 session.library=GetModuleHandleW(L"bas60.dll");check(session.library!=nullptr,"scene region BAS60");load_area(session,2,file);
 check(sym<Count>(session.library,"?_GetRegNum@C60_WorkArea@@QAEFPAJ0@Z")(&session.area,&active,&upper)>0
  &&active>=1&&active<=20000&&upper>active&&upper<=200001,"scene native region count");return true;
}
long region_color(Session& session,long id){
 using Info=short(__stdcall*)(int,long,void*);unsigned char info[64]{};
 check(sym<Info>(session.library,MAKEINTRESOURCEA(45))(session.area.area,id,info)>0,"scene native region fill style");long color=0;memcpy(&color,info,4);return color;
}
void saved_regions(const std::wstring& folder){
 Session session;long active=0,upper=0;bool present=open_regions(folder,session,active,upper);
 bool mesh=GetFileAttributesW((folder+L"/regions.mesh").c_str())!=INVALID_FILE_ATTRIBUTES;check(present==mesh,"scene region members incomplete");if(!present)return;
 auto name=ansi((folder+L"/regions.mesh").c_str());std::ifstream input(name.data());input.imbue(std::locale::classic());std::string magic,extra;int count=0;input>>magic>>count;
 check(input&&magic=="MGSR0001"&&count==active,"scene region mesh header");std::set<int> ids;size_t total=0;
 for(int i=0;i<count;++i){int id=0,n=0,m=0;input>>id>>n>>m;
  check(input&&id>=1&&id<upper&&ids.insert(id).second&&n>=3&&m>=3&&m<=100000&&total+n<=100000,"scene region mesh readback counts");total+=n;
  for(int j=0;j<n;++j){double x=0,y=0;check(bool(input>>x>>y)&&std::isfinite(x)&&std::isfinite(y),"scene region mesh readback coordinate");}
  for(int j=0;j<m;++j){unsigned a=0,b=0;check(bool(input>>a>>b)&&a<unsigned(n)&&b<unsigned(n)&&a!=b,"scene region mesh readback edge");}
  printf("{\"event\":\"scene_region_record\",\"record_id\":%d,\"fill_color\":%ld}\n",id,region_color(session,id));
 }
 check(!(input>>extra)&&input.eof(),"scene region mesh trailing content");
 printf("{\"event\":\"scene_regions_readback\",\"active\":%ld,\"record_upper_bound\":%ld,\"mesh_vertices\":%u}\n",active,upper,unsigned(total));fflush(stdout);
 session.close();saved_file(folder,L"regions.wp");saved_file(folder,L"regions.mesh");
}
class NativeRegions;
static NativeRegions* scene_regions=nullptr;
class NativeRegions {
 Session session;std::vector<RegionMesh> regions;void* drawing_=nullptr;bool visible=false;unsigned char line_flag=0;
 float opacity=1,offset=0;double distance=0;
public:
 ~NativeRegions(){if(scene_regions==this)scene_regions=nullptr;}
 bool owns(void* drawing)const{return drawing_==drawing&&visible&&opacity>0;}
 void before(){static_cast<unsigned char*>(drawing_)[82]=0;}
 void restore(){static_cast<unsigned char*>(drawing_)[82]=line_flag;}
 void draw(HMODULE module,void* state){
  glPushAttrib(GL_ALL_ATTRIB_BITS);glPushClientAttrib(GL_CLIENT_ALL_ATTRIB_BITS);
  struct RestoreGL {~RestoreGL(){glPopClientAttrib();glPopAttrib();}} restore_gl;
  glDisable(GL_LIGHTING);glDisable(GL_TEXTURE_2D);glDisable(GL_FOG);glDisable(GL_CULL_FACE);glEnable(GL_DEPTH_TEST);glDepthFunc(GL_LEQUAL);glDepthMask(GL_FALSE);
  glEnable(GL_POLYGON_OFFSET_FILL);glPolygonOffset(-1.f,-1.f);glPolygonMode(GL_FRONT_AND_BACK,GL_FILL);
  if(opacity<1){glEnable(GL_BLEND);glBlendFunc(GL_SRC_ALPHA,GL_ONE_MINUS_SRC_ALPHA);}else glDisable(GL_BLEND);
  int mode=0;memcpy(&mode,static_cast<unsigned char*>(state)+1065229,4);double eye[2]{};
  if(mode==1){float origin[2]{},scale[2]{},local[2]{};memcpy(origin,static_cast<unsigned char*>(state)+1065047,8);memcpy(scale,static_cast<unsigned char*>(state)+1065095,8);memcpy(local,static_cast<unsigned char*>(state)+1065123,8);
   check(scale[0]>0&&scale[1]>0,"scene region camera scales");for(int i=0;i<2;++i)eye[i]=double(local[i])/scale[i]+origin[i];}
  using Distance=double(__stdcall*)(const double*,int,long);
  for(const auto& region:regions){
   if(mode==1){double actual=sym<Distance>(session.library,MAKEINTRESOURCEA(644))(eye,session.area.area,region.id);check(std::isfinite(actual)&&actual>=0,"scene native region distance");if(actual>distance)continue;}
   glColor4f(GetRValue(region.rgb)/255.f,GetGValue(region.rgb)/255.f,GetBValue(region.rgb)/255.f,opacity);glBegin(GL_TRIANGLES);
   for(const auto& triangle:region.triangles)for(unsigned index:triangle)glVertex3fv(region.vertices[index].data());glEnd();
  }
 }
 void after(HMODULE module,void* state){
  draw(module,state);
  if(line_flag){glPushAttrib(GL_ALL_ATTRIB_BITS);glPushClientAttrib(GL_CLIENT_ALL_ATTRIB_BITS);
   struct RestoreGL {~RestoreGL(){glPopClientAttrib();glPopAttrib();}} restore_gl;
   using Render=long(__thiscall*)(void*,void*);check(reinterpret_cast<Render>(reinterpret_cast<unsigned char*>(module)+0x1c840)(drawing_,state)==1,"scene original WL draw after regions");
  }
  check(glGetError()==GL_NO_ERROR,"scene region composition OpenGL state");
 }
 void bind(const std::wstring& folder,HMODULE module,void* drawing,void* state,const DemParameters& dem){
  long active=0,upper=0;if(!open_regions(folder,session,active,upper))return;
  auto settings_name=ansi((folder+L"/regions.txt").c_str());std::ifstream settings(settings_name.data());settings.imbue(std::locale::classic());
  int version=0,count=0,enabled=0;double alpha=0,zoffset=0;std::string extra;
  settings>>version>>count>>enabled>>alpha>>zoffset>>distance;
  check(settings&&!(settings>>extra)&&version==1&&count==active&&(enabled==0||enabled==1)&&std::isfinite(alpha)&&alpha>=0&&alpha<=1
   &&std::isfinite(zoffset)&&fabs(zoffset)<=1e6&&std::isfinite(distance)&&distance>0,"scene region display settings");visible=enabled!=0;opacity=float(alpha);offset=float(zoffset);
  auto name=ansi((folder+L"/regions.mesh").c_str());std::ifstream input(name.data());input.imbue(std::locale::classic());std::string magic;int mesh_count=0;input>>magic>>mesh_count;
  check(input&&magic=="MGSR0001"&&mesh_count==count,"scene region mesh header");
  float origin[2]{},scale[3]{},move=0;memcpy(origin,static_cast<unsigned char*>(state)+1065047,8);memcpy(scale,static_cast<unsigned char*>(state)+1065095,12);memcpy(&move,static_cast<unsigned char*>(state)+1065151,4);
  for(float value:scale)check(std::isfinite(value)&&value>0,"scene region model scale");
  using Height=double(__cdecl*)(void*,const double*);auto height=reinterpret_cast<Height>(reinterpret_cast<unsigned char*>(module)+0x1b640);
  using Color=short(__stdcall*)(long,void*,void*,void*);auto palette=GetModuleHandleW(L"mapdsp60.dll");check(palette!=nullptr,"scene region palette module");
  size_t total=0,faces=0;std::set<int> ids;
  for(int feature=0;feature<count;++feature){int id=0,n=0,m=0;input>>id>>n>>m;
   check(input&&id>=1&&id<upper&&ids.insert(id).second&&n>=3&&m>=3&&m<=100000&&total+n<=100000,"scene region mesh counts");total+=n;
   RegionMesh region{};region.id=id;std::vector<CDT::V2d<double>> xy;std::vector<CDT::Edge> constraints;std::set<std::pair<double,double>> unique;
   double low=0,high=0;
   for(int i=0;i<n;++i){double point[2]{};input>>point[0]>>point[1];
    check(input&&std::isfinite(point[0])&&std::isfinite(point[1])&&unique.insert({point[0],point[1]}).second
     &&point[0]>=dem.xmin&&point[0]<=dem.xmax&&point[1]>=dem.ymin&&point[1]<=dem.ymax,"scene region finite unique in-bounds mesh point");
    xy.emplace_back(point[0],point[1]);double z=height(state,point);double tolerance=std::max(1e-4,(dem.zmax-dem.zmin)*1e-6);
    check(std::isfinite(z)&&z>=dem.zmin-tolerance&&z<=dem.zmax+tolerance,"scene original region terrain height");low=i?std::min(low,z):z;high=i?std::max(high,z):z;
    std::array<float,3> vertex{float(point[0]-origin[0])*scale[0],float(point[1]-origin[1])*scale[1],float(z+offset)*scale[2]+move};
    for(float value:vertex)check(std::isfinite(value),"scene region finite model vertex");region.vertices.push_back(vertex);
   }
   for(int i=0;i<m;++i){unsigned a=0,b=0;input>>a>>b;check(input&&a<unsigned(n)&&b<unsigned(n)&&a!=b,"scene region boundary constraint");constraints.emplace_back(a,b);}
   CDT::Triangulation<double> mesh(CDT::VertexInsertionOrder::AsProvided);mesh.insertVertices(xy);mesh.insertEdges(constraints);mesh.eraseOuterTrianglesAndHoles();
   check(mesh.vertices.size()==xy.size()&&!mesh.triangles.empty()&&mesh.triangles.size()<=2*xy.size(),"scene CDT region triangle count");double area=0;
   for(const auto& triangle:mesh.triangles){std::array<unsigned,3> vertices{};
    for(int i=0;i<3;++i){unsigned index=triangle.vertices[i];check(index<xy.size()&&mesh.vertices[index].x==xy[index].x&&mesh.vertices[index].y==xy[index].y,"scene CDT region vertex identity");vertices[i]=index;}
    const auto& a=xy[vertices[0]];const auto& b=xy[vertices[1]];const auto& c=xy[vertices[2]];area+=fabs((b.x-a.x)*(c.y-a.y)-(c.x-a.x)*(b.y-a.y))*.5;region.triangles.push_back(vertices);
   }
   region.color=region_color(session,id);unsigned char r[8]{},g[8]{},b[8]{};
   check(sym<Color>(palette,MAKEINTRESOURCEA(403))(region.color,r,g,b)>0,"scene region palette colour unavailable");region.rgb=RGB(r[0],g[0],b[0]);faces+=region.triangles.size();
   printf("{\"event\":\"scene_region_mesh\",\"record_id\":%d,\"vertices\":%u,\"triangles\":%u,\"area\":%.17g,\"fill_color\":%ld,\"rgb\":[%u,%u,%u],\"height_range\":[%.17g,%.17g],\"native_height_move_model\":%.9g}\n",id,unsigned(region.vertices.size()),unsigned(region.triangles.size()),area,region.color,r[0],g[0],b[0],low,high,move);fflush(stdout);regions.push_back(std::move(region));
  }
  check(!(input>>extra)&&input.eof(),"scene region mesh trailing content");check(scene_regions==nullptr,"scene region context already owned");drawing_=drawing;line_flag=static_cast<unsigned char*>(drawing)[82];scene_regions=this;
  printf("{\"event\":\"scene_regions_bound\",\"active\":%ld,\"record_upper_bound\":%ld,\"mesh_vertices\":%u,\"triangles\":%u,\"visible\":%s,\"opacity\":%.9g,\"licensed_triangulator_called\":false}\n",active,upper,unsigned(total),unsigned(faces),visible?"true":"false",opacity);fflush(stdout);
 }
};
struct RegionBaseDraw {
 NativeRegions* context=nullptr;
 RegionBaseDraw(void* drawing){if(scene_regions&&scene_regions->owns(drawing)){context=scene_regions;context->before();}}
 void restore(){if(context)context->restore();}
 ~RegionBaseDraw(){restore();}
};
