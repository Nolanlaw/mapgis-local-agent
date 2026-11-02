// Included inside native_tin_scene. Uses the existing original renderer and VFW.
#pragma once
Camera record_flight(const std::wstring& folder,HMODULE module,HMODULE base,void* view,int width,int height,const double* bounds){
 auto settings_name=ansi((folder+L"/flight.txt").c_str());std::ifstream input(settings_name.data());input.imbue(std::locale::classic());
 int knots=0,start=0,frames=0,fps=0;float speed=0;input>>knots>>start>>frames>>fps>>speed;
 check(input&&knots>=2&&knots<=100&&start>=0&&start<=999&&frames>=0&&frames<=1000&&fps>=1&&fps<=60&&std::isfinite(speed)&&speed>0&&speed<=1000,"TIN flight parameters");
 std::vector<float> points(size_t(knots)*3);for(float& value:points)input>>value;std::string extra;check(input&&!(input>>extra),"TIN flight route input");
 for(float value:points)check(std::isfinite(value),"TIN flight finite knot");
 using Count=long(__thiscall*)(void*,const float*,const float*,float);auto line_count=sym<Count>(base,"?CalLineFrameNum@C3DView@@AAEJPAUG3D_VERTEX@@0M@Z");
 long capacity_bound=0,previous_count=0;for(int i=1;i<knots;++i){auto a=points.data()+3*(i-1),b=points.data()+3*i;check(a[0]!=b[0]||a[1]!=b[1],"TIN flight distinct horizontal knots");long count=line_count(view,a,b,speed);check(count>=2&&count<=1000,"TIN flight native segment count");capacity_bound+=count;if(previous_count)capacity_bound+=long(double(previous_count+count)*0.3);previous_count=count;check(capacity_bound<=1000,"TIN flight native allocation bound");}
 auto pat=ansi((folder+L"/flight.pat").c_str());check(GetFileAttributesA(pat.data())==INVALID_FILE_ATTRIBUTES,"TIN flight PAT output exists");
 std::ofstream path(pat.data(),std::ios::binary);check(bool(path),"TIN flight PAT open");path<<"HEIGHTTYPE:0\r\nAZIMUTH:0.0\r\nELEVATION:0.0\r\nDOTCOUNT:"<<knots<<"\r\n";
 for(int i=0;i<knots;++i){char row[128]{};int size=snprintf(row,sizeof(row),"%.5f,%.5f,%.5f,%.5f\r\n",double(points[3*i]),double(points[3*i+1]),double(points[3*i+2]),double(speed));check(size>0&&size<50,"TIN flight original reader row bound");path.write(row,size);}path.close();check(bool(path),"TIN flight PAT saved");
 using Load=long(__thiscall*)(void*,const char*);check(sym<Load>(base,"?PlayPatPathFile@C3DView@@QAEJPAD@Z")(view,pat.data())>0,"original TIN PAT interpolation");
 auto object=static_cast<unsigned char*>(view);int capacity=0,count=0;double* positions=nullptr;memcpy(&capacity,object+584*4,4);memcpy(&count,object+589*4,4);memcpy(&positions,object+585*4,4);
 check(positions&&count>=2&&count<=capacity&&capacity<=1000,"TIN flight generated route bound");
 for(int i=0;i<count;++i){for(int j=0;j<6;++j)check(std::isfinite(positions[6*i+j]),"TIN flight finite interpolated camera");check(positions[6*i]!=positions[6*i+3]||positions[6*i+1]!=positions[6*i+4],"TIN flight nonvertical interpolated camera");}
 check(start<count,"TIN flight starting index");if(!frames)frames=count-start;check(frames>=1&&frames<=1000&&start+frames<=count&&start+frames<=1000,"TIN flight selected frame bound");
 int stride=(width*3+3)&~3;check(static_cast<unsigned long long>(stride)*height*frames<=1024ull*1024*1024-65536,"TIN flight AVI size bound");
 int index=0;memcpy(object+589*4,&index,4);using Flag=long(__thiscall*)(void*,bool);check(sym<Flag>(base,"?SetPlayFlag@C3DView@@QAEJ_N@Z")(view,true)>0,"TIN flight start");
 native_scene::NativeAvi avi;auto filename=ansi((folder+L"/flight.avi").c_str());check(GetFileAttributesA(filename.data())==INVALID_FILE_ATTRIBUTES,"TIN flight AVI output exists");check(AVIFileOpenA(&avi.file,filename.data(),OF_CREATE|OF_WRITE,nullptr)==0,"TIN flight AVI create");
 AVISTREAMINFOA info{};info.fccType=streamtypeVIDEO;info.fccHandler=mmioFOURCC('D','I','B',' ');info.dwScale=1;info.dwRate=fps;info.dwSuggestedBufferSize=stride*height;info.dwQuality=DWORD(-1);SetRect(&info.rcFrame,0,0,width,height);
 check(AVIFileCreateStreamA(avi.file,&avi.stream,&info)==0,"TIN flight AVI stream");BITMAPINFOHEADER format{};format.biSize=sizeof(format);format.biWidth=width;format.biHeight=height;format.biPlanes=1;format.biBitCount=24;format.biCompression=BI_RGB;format.biSizeImage=stride*height;check(AVIStreamSetFormat(avi.stream,0,&format,sizeof(format))==0,"TIN flight AVI format");
 std::vector<unsigned char> rgb(size_t(width)*height*3),bgr(size_t(stride)*height),top(rgb.size());using Tick=void(__thiscall*)(void*,unsigned);using Frame=long(__thiscall*)(void*);Camera final_camera{};
 for(int step=0;step<start+frames;++step){
  sym<Tick>(base,"?OnTimer@C3DView@@IAEXI@Z")(view,1024);memcpy(&index,object+589*4,4);check(index==step+1,"TIN flight one original step per frame");
  auto data=state(view);memcpy(data+1053723,object+173,12);memcpy(data+1053735,object+185,12);memcpy(data+1053711,object+197,12);final_camera=camera(view,bounds);validate(final_camera);
  check(sym<Frame>(base,"?BeginRenderScene@C3DView@@UAEJXZ")(view)>0,"TIN flight original begin");check(sym<Frame>(module,"?RenderScene@CTin3DView@@EAEJXZ")(view)>0,"TIN flight original render");
  GLint viewport[4]{};glGetIntegerv(GL_VIEWPORT,viewport);check(viewport[2]==width&&viewport[3]==height,"TIN flight viewport");glReadBuffer(GL_BACK);glPixelStorei(GL_PACK_ALIGNMENT,1);glPixelStorei(GL_PACK_ROW_LENGTH,0);glPixelStorei(GL_PACK_SKIP_ROWS,0);glPixelStorei(GL_PACK_SKIP_PIXELS,0);glFinish();glReadPixels(0,0,width,height,GL_RGB,GL_UNSIGNED_BYTE,rgb.data());check(glGetError()==GL_NO_ERROR,"TIN flight RGB readback");sym<Frame>(base,"?EndRenderScene@C3DView@@UAEJXZ")(view);
  if(step<start)continue;int frame=step-start;std::fill(bgr.begin(),bgr.end(),0);for(int y=0;y<height;++y){memcpy(top.data()+size_t(y)*width*3,rgb.data()+size_t(height-1-y)*width*3,size_t(width)*3);for(int x=0;x<width;++x)for(int band=0;band<3;++band)bgr[size_t(y)*stride+3*x+band]=rgb[(size_t(y)*width+x)*3+2-band];}
  long samples=0,bytes=0;check(AVIStreamWrite(avi.stream,frame,1,bgr.data(),long(bgr.size()),AVIIF_KEYFRAME,&samples,&bytes)==0&&samples==1&&bytes==long(bgr.size()),"TIN flight AVI frame write");
  printf("{\"event\":\"tin_flight_frame\",\"index\":%d,\"route_index\":%d,\"next_native_index\":%d,\"eye_model\":[%.9g,%.9g,%.9g],\"target_model\":[%.9g,%.9g,%.9g],\"rgb_sha256\":\"%s\"}\n",frame,step,index,final_camera.eye[0],final_camera.eye[1],final_camera.eye[2],final_camera.target[0],final_camera.target[1],final_camera.target[2],native_scene::flight_pixel_sha(top).c_str());fflush(stdout);
 }
 avi.close();native_scene::write_frame(folder+L"/scene.tiles",width,height,rgb);using Stop=long(__thiscall*)(void*);check(sym<Stop>(base,"?StopPlayPath@C3DView@@QAEJXZ")(view)>0,"TIN flight stop");
 printf("{\"event\":\"tin_flight_written\",\"generated_frames\":%d,\"capacity\":%d,\"frame_start\":%d,\"frames\":%d,\"fps\":%d,\"width\":%d,\"height\":%d,\"speed\":%.9g,\"original_save_called\":false}\n",count,capacity,start,frames,fps,width,height,speed);fflush(stdout);
 native_scene::saved_file(folder,L"flight.pat");native_scene::saved_file(folder,L"flight.avi");return final_camera;
}
