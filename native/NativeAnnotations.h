// Original WT annotations and GDI glyphs, isolated to this owned scene.
#pragma once
#pragma pack(push,1)
struct NativeNote {int id;float offset;unsigned char enabled;double distance;};
struct NativeNoteFont {char face[64];unsigned char flags;short points;};
#pragma pack(pop)
static_assert(sizeof(NativeNote)==17&&sizeof(NativeNoteFont)==67,"original note/font x86 ABI");

std::string note_hex(const char* data,size_t count){
 std::string result;result.reserve(count*2);for(size_t i=0;i<count;++i){char value[3];sprintf_s(value,"%02x",static_cast<unsigned char>(data[i]));result+=value;}return result;
}
bool open_notes(const std::wstring& folder,Session& session,long& active,long& upper){
 auto source=folder+L"/notes.wt";if(GetFileAttributesW(source.c_str())==INVALID_FILE_ATTRIBUTES)return false;
 session.library=GetModuleHandleW(L"bas60.dll");check(session.library!=nullptr,"scene annotations BAS60");load_area(session,1,source);
 check(sym<Count>(session.library,"?_GetPntNum@C60_WorkArea@@QAEFPAJ0@Z")(&session.area,&active,&upper)>0
  &&active>=1&&active<=20000&&upper>active&&upper<=200001,"scene native WT count");return true;
}
void saved_notes(const std::wstring& folder){
 Session session;long active=0,upper=0;if(!open_notes(folder,session,active,upper))return;
 using Get=short(__stdcall*)(int,long,void*,char**,short*,void*,int);auto get=sym<Get>(session.library,MAKEINTRESOURCEA(1557));long count=0;
 for(long id=1;id<upper;++id){double xyz[3]{};char* text=nullptr;short bytes=0;unsigned char info[94]{};
  if(get(session.area.area,id,xyz,&text,&bytes,info,3)<=0)continue;
  check(info[0]==0&&text&&bytes>0&&bytes<=32767&&strlen(text)==size_t(bytes),"scene plain native annotation");
  for(double value:xyz)check(std::isfinite(value),"scene annotation anchor");
  printf("{\"event\":\"scene_annotation_record\",\"record_id\":%ld,\"xy\":[%.17g,%.17g],\"text_hex\":\"%s\"}\n",id,xyz[0],xyz[1],note_hex(text,bytes).c_str());++count;
 }
 check(count==active,"scene native annotation row mapping");
 printf("{\"event\":\"scene_annotations_readback\",\"active\":%ld,\"record_upper_bound\":%ld}\n",active,upper);fflush(stdout);
 session.close();saved_file(folder,L"notes.wt");
}

class NativeAnnotations;
static NativeAnnotations* scene_annotations=nullptr;
class NativeAnnotations {
 Session session;std::vector<NativeNote> notes;std::vector<bool> black;void* drawing_=nullptr;bool visible=false;
 static int CALLBACK found_font(const LOGFONTA*,const TEXTMETRICA*,DWORD,LPARAM parameter){*reinterpret_cast<bool*>(parameter)=true;return 0;}
 COLORREF color(long id){
  unsigned char info[94]{};check(sym<PointInfo>(session.library,MAKEINTRESOURCEA(69))(session.area.area,id,info)>0&&info[0]==0,"scene ordinary WT annotation required");
  long value=0;memcpy(&value,info+44,4);auto colors=GetModuleHandleW(L"mapdsp60.dll");check(colors!=nullptr,"scene annotation palette");
  using Color=short(__stdcall*)(long,void*,void*,void*);unsigned char r[8]{},g[8]{},b[8]{};
  return sym<Color>(colors,MAKEINTRESOURCEA(403))(value,r,g,b)?RGB(r[0],g[0],b[0]):RGB(0,0,255);
 }
 void glyph_bounds(HMODULE module,const NativeNoteFont& font,int& width,int& height){
  unsigned char* atlas=nullptr;memcpy(&atlas,reinterpret_cast<unsigned char*>(module)+0x36450,4);check(atlas!=nullptr,"scene original note atlas");
  HDC dc=nullptr;short w=0,h=0;void* bitmap=nullptr;memcpy(&dc,atlas+1121,4);memcpy(&w,atlas+1165,2);memcpy(&h,atlas+1167,2);memcpy(&bitmap,atlas+1169,4);
  check(dc&&bitmap&&(w==128||w==256||w==512)&&h==w,"scene original note atlas bounds");width=w;height=h;
  LOGFONTA requested{};requested.lfCharSet=GB2312_CHARSET;strcpy_s(requested.lfFaceName,font.face);bool found=false;
  EnumFontFamiliesExA(dc,&requested,found_font,reinterpret_cast<LPARAM>(&found),0);check(found,"scene annotation font unavailable; choose an installed GB2312 font");
  using Get=short(__stdcall*)(int,long,void*,char**,short*,void*,int);auto get=sym<Get>(session.library,MAKEINTRESOURCEA(1557));
  for(const auto& note:notes){
   char* text=nullptr;short count=0;unsigned char info[94]{};double xyz[3]{};
   check(get(session.area.area,note.id,xyz,&text,&count,info,3)>0&&info[0]==0&&text&&count>0&&strlen(text)==size_t(count),"scene annotation text readback");
   int length=MultiByteToWideChar(936,MB_ERR_INVALID_CHARS,text,count,nullptr,0);check(length>0,"scene annotation CP936 text");std::vector<wchar_t> wide(length);std::vector<WORD> glyphs(length);
   check(MultiByteToWideChar(936,MB_ERR_INVALID_CHARS,text,count,wide.data(),length)==length,"scene annotation Unicode conversion");
   check(GetGlyphIndicesW(dc,wide.data(),length,glyphs.data(),GGI_MARK_NONEXISTING_GLYPHS)!=GDI_ERROR,"scene annotation glyph coverage query");
   for(WORD glyph:glyphs)check(glyph!=0xffff,"scene annotation font missing glyph");
   int x=0,y=0;for(int i=0;i<count;){unsigned char c=text[i];int bytes=c>=128?2:1;
    check(c>=32&&c!=127&&(bytes==1||(i+1<count&&c>=0xa1&&c<=0xf7&&static_cast<unsigned char>(text[i+1])>=0xa1&&static_cast<unsigned char>(text[i+1])<=0xfe)),"scene plain GB2312 glyph required");
    SIZE size{};check(GetTextExtentPoint32A(dc,text+i,bytes,&size)&&size.cx>=0&&size.cy>0&&size.cx+1<=w,"scene annotation glyph width");
    if(x+size.cx+1>w){x=0;y+=size.cy+1;}check(y+size.cy<=h,"scene annotation exceeds original glyph atlas; split the label");x+=size.cx+1;i+=bytes;
   }
  }
 }
public:
 ~NativeAnnotations(){if(scene_annotations==this)scene_annotations=nullptr;}
 bool owns(void* drawing)const{return drawing_==drawing;}
 void before(){static_cast<unsigned char*>(drawing_)[81]=0;}
 void restore(){static_cast<unsigned char*>(drawing_)[81]=1;}
 void after(HMODULE module,void* state){
  restore();if(!visible)return;
  glPushAttrib(GL_ALL_ATTRIB_BITS);glPushClientAttrib(GL_CLIENT_ALL_ATTRIB_BITS);
  struct Restore {NativeAnnotations* self;~Restore(){glPopClientAttrib();glPopAttrib();for(auto& note:self->notes)note.enabled=self->visible;}} restore_state{this};
  glDisable(GL_LIGHTING);glDisable(GL_BLEND);glAlphaFunc(GL_GREATER,0.f);
  using Render=long(__thiscall*)(void*,void*);auto render=reinterpret_cast<Render>(reinterpret_cast<unsigned char*>(module)+0x1cee0);
  unsigned char* atlas=nullptr;memcpy(&atlas,reinterpret_cast<unsigned char*>(module)+0x36450,4);check(atlas!=nullptr,"scene owned annotation atlas");
  void* descriptors=nullptr;float* positions=nullptr;int count=0;memcpy(&descriptors,atlas+1153,4);memcpy(&count,atlas+1157,4);memcpy(&positions,atlas+1161,4);
  check(descriptors==notes.data()&&count==int(notes.size())&&positions,"scene owned annotation descriptor binding");
  struct Slice {unsigned char* atlas;void* descriptors;float* positions;int count;~Slice(){memcpy(atlas+1153,&descriptors,4);memcpy(atlas+1157,&count,4);memcpy(atlas+1161,&positions,4);}} slice{atlas,descriptors,positions,count};
  // Render contiguous colour groups in physical-record order. The original
  // loader retains this caller-owned descriptor array; each temporary slice
  // pairs it with the same owned native XYZ slice, without reallocating either.
  for(size_t start=0;start<notes.size();){size_t end=start+1;while(end<notes.size()&&black[end]==black[start])++end;
   NativeNote* current=notes.data()+start;float* xyz=positions+3*start;int length=int(end-start);
   memcpy(atlas+1153,&current,4);memcpy(atlas+1157,&length,4);memcpy(atlas+1161,&xyz,4);
   float value=black[start]?0.f:1.f;glColor4f(value,value,value,1.f);check(render(drawing_,state)==1,"scene original annotation draw");start=end;
  }
 }
 void bind(const std::wstring& folder,HMODULE module,void* drawing,void* state){
  auto source=folder+L"/notes.wt";if(GetFileAttributesW(source.c_str())==INVALID_FILE_ATTRIBUTES)return;
  auto display=folder+L"/notes-display.wt";check(CopyFileW(source.c_str(),display.c_str(),TRUE)!=0,"scene owned annotation display copy");
  auto settings_name=ansi((folder+L"/annotations.txt").c_str());std::ifstream input(settings_name.data());input.imbue(std::locale::classic());
  int version=0,count=0,flags=0,points=0,enabled=0;double offset=0,distance=0;std::string face,extra;
  input>>version>>count>>flags>>points>>offset>>distance>>enabled>>face;
  check(input&&version==1&&count>=1&&count<=20000&&flags>=0&&flags<=3&&points>=1&&points<=30&&std::isfinite(offset)&&fabs(offset)<=1e6&&std::isfinite(distance)&&distance>0
   &&(enabled==0||enabled==1)&&face.size()>=2&&face.size()<=62&&face.size()%2==0&&face.find_first_not_of("0123456789abcdef")==std::string::npos,"scene annotation settings");
  NativeNoteFont font{},readback{};font.flags=static_cast<unsigned char>(flags);font.points=short(points);
  for(size_t i=0;i<face.size();i+=2){unsigned char c=static_cast<unsigned char>(strtoul(face.substr(i,2).c_str(),nullptr,16));check(c>=32,"scene annotation font face");font.face[i/2]=char(c);}
  std::set<int> ids;for(int i=0;i<count;++i){int id=0;check(bool(input>>id)&&id>=1&&id<=200000&&ids.insert(id).second,"scene annotation physical IDs");notes.push_back({id,float(offset),static_cast<unsigned char>(enabled),distance});}
  check(!(input>>extra)&&input.eof(),"scene annotation settings trailing content");
  session.library=GetModuleHandleW(L"bas60.dll");check(session.library!=nullptr,"scene annotation native library");load_area(session,1,display);long active=0,upper=0;
  check(sym<Count>(session.library,"?_GetPntNum@C60_WorkArea@@QAEFPAJ0@Z")(&session.area,&active,&upper)>0&&active==count&&upper<=200001,"scene annotation active count");
  std::vector<long> masked;for(const auto& note:notes){bool value=color(note.id)==RGB(0,0,0);black.push_back(value);if(value)masked.push_back(note.id);}
  // The original atlas makes RGB0 transparent. Change only the owned display
  // work area to a verified nonblack mask colour, then modulate those glyphs
  // with black. The canonical WT bytes and original DLL stay untouched.
  for(size_t start=0;start<masked.size();start+=1000){std::ostringstream commands;
   for(size_t i=start;i<std::min(start+1000,masked.size());++i)commands<<masked[i]<<" 1 0 4\n";
   std::istringstream updates(commands.str());edit_area(session,1,L"point.style",updates);
  }
  for(long id:masked)check(color(id)!=RGB(0,0,0),"scene annotation mask colour unavailable");
  using Font=long(__cdecl*)(void*,void*,void*);check(sym<Font>(module,"_SetFontParam")(drawing,state,&font)==0&&sym<Font>(module,"_GetFontParam")(drawing,state,&readback)==0&&!memcmp(&font,&readback,sizeof(font)),"scene original annotation font roundtrip");
  using Load=long(__cdecl*)(void*,void*,int,void*,int);check(sym<Load>(module,"_tnLoadMapNote")(drawing,state,session.area.area,notes.data(),count)==0,"scene original WT annotation load");
  int width=0,height=0;glyph_bounds(module,font,width,height);check(static_cast<unsigned char*>(drawing)[81]==1,"scene annotation display state");
  check(scene_annotations==nullptr,"scene annotation context already owned");drawing_=drawing;visible=enabled!=0;scene_annotations=this;
  printf("{\"event\":\"scene_annotations_bound\",\"active\":%ld,\"record_upper_bound\":%ld,\"font_points\":%d,\"font_flags\":%d,\"atlas\":[%d,%d],\"black_mask_records\":%u,\"visible\":%s,\"glyph_coverage_checked\":true,\"atlas_fit_checked\":true}\n",active,upper,points,flags,width,height,unsigned(masked.size()),visible?"true":"false");fflush(stdout);
 }
};
struct AnnotationBaseDraw {
 NativeAnnotations* context=nullptr;
 AnnotationBaseDraw(void* drawing){if(scene_annotations&&scene_annotations->owns(drawing)){context=scene_annotations;context->before();}}
 void restore(){if(context)context->restore();}
 ~AnnotationBaseDraw(){restore();}
};
