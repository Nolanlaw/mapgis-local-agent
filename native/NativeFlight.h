// Included inside native_scene, after the original RGB frame capture helpers.
#pragma once
using FlightRange=long(__cdecl*)(void*,void*,float*,float*);
std::pair<float,float> flight_speed_range(HMODULE module,void* drawing,void* state){
 float low=0,high=0;check(sym<FlightRange>(module,"_GetFlySpeedRange")(drawing,state,&low,&high)==0&&std::isfinite(low)&&std::isfinite(high),"scene flight speed range");return {low,high};
}
struct NativeAvi {
 PAVIFILE file=nullptr;PAVISTREAM stream=nullptr;bool initialized=true;
 NativeAvi(){AVIFileInit();}
 void close(){if(stream){AVIStreamRelease(stream);stream=nullptr;}if(file){AVIFileRelease(file);file=nullptr;}if(initialized){AVIFileExit();initialized=false;}}
 ~NativeAvi(){close();}
};
std::string flight_pixel_sha(const std::vector<unsigned char>& bytes){
 HCRYPTPROV provider=0;HCRYPTHASH hash=0;BYTE result[32]{};DWORD size=32;
 bool ok=CryptAcquireContextW(&provider,nullptr,nullptr,PROV_RSA_AES,CRYPT_VERIFYCONTEXT)&&CryptCreateHash(provider,CALG_SHA_256,0,0,&hash)
  &&CryptHashData(hash,bytes.data(),DWORD(bytes.size()),0)&&CryptGetHashParam(hash,HP_HASHVAL,result,&size,0);
 if(hash)CryptDestroyHash(hash);if(provider)CryptReleaseContext(provider,0);check(ok&&size==32,"flight RGB SHA256");
 std::string output;for(BYTE byte:result){char pair[3];sprintf_s(pair,"%02x",byte);output+=pair;}return output;
}
NativeCameraPose record_flight(const std::wstring& folder,HMODULE module,void* drawing,void* state,const DemParameters& dem,int width,int height){
 auto settings_name=ansi((folder+L"/flight.txt").c_str());std::ifstream input(settings_name.data());input.imbue(std::locale::classic());
 int knots=0,start=0,frames=0,fps=0;float speed=0;input>>knots>>start>>frames>>fps>>speed;
 check(input&&knots>=2&&knots<=5000&&start>=0&&start<=999&&frames>=0&&frames<=1000&&fps>=1&&fps<=60&&std::isfinite(speed)&&speed>0,"flight input parameters");
 std::vector<float> points(size_t(knots)*2);for(float& value:points)input>>value;std::string extra;check(input&&!(input>>extra),"flight route input");
 for(int i=0;i<knots;++i){float x=points[2*i],y=points[2*i+1];check(std::isfinite(x)&&std::isfinite(y)&&x>=dem.xmin&&x<=dem.xmax&&y>=dem.ymin&&y<=dem.ymax,"flight knot within terrain");if(i)check(x!=points[2*i-2]||y!=points[2*i-1],"flight distinct adjacent knots");}
 auto limits=flight_speed_range(module,drawing,state);
 printf("{\"event\":\"flight_speed_limits\",\"range\":[%.9g,%.9g]}\n",limits.first,limits.second);fflush(stdout);
 check(limits.first>0&&limits.second>=limits.first&&speed>=limits.first&&speed<=limits.second,"flight requested speed outside original range");
 check(sym<SetRatio>(module,"_SetFlySpeed")(drawing,state,speed)==0,"flight speed set");float actual_speed=0;check(sym<GetRatio>(module,"_GetFlySpeed")(drawing,state,&actual_speed)==0&&actual_speed==speed,"flight speed readback");
 using Route=long(__cdecl*)(void*,void*,const float*,int);check(sym<Route>(module,"_SetFlyRoutin")(drawing,state,points.data(),knots)==0,"original flight route");
 auto data=static_cast<unsigned char*>(state);int count=0,index=0;unsigned char* route=nullptr;memcpy(&count,data+1065195,4);memcpy(&index,data+1065199,4);memcpy(&route,data+1065191,4);
 check(count>=2&&count<=100000&&route&&index==0,"flight generated route");
 float xmin=0,xmax=0,ymin=0,ymax=0;memcpy(&xmin,data+1065055,4);memcpy(&xmax,data+1065059,4);memcpy(&ymin,data+1065063,4);memcpy(&ymax,data+1065067,4);
 for(int i=0;i<count;++i){float x=0,y=0;memcpy(&x,route+21*i,4);memcpy(&y,route+21*i+4,4);check(std::isfinite(x)&&std::isfinite(y)&&x>=xmin&&x<=xmax&&y>=ymin&&y<=ymax,"flight interpolated route outside terrain");}
 check(start<count,"flight starting index");if(!frames)frames=count-start;check(frames>=1&&frames<=1000&&start+frames<=count&&start+frames<=1000,"flight selected frame count");
 int stride=(width*3+3)&~3;check(static_cast<unsigned long long>(stride)*height*frames<=1024ull*1024*1024-65536,"flight uncompressed AVI exceeds1GiB");
 check(sym<SetDisplay>(module,"_SetFlyFlag")(drawing,state,2)==0,"flight custom flag");using Flag=long(__cdecl*)(void*,void*,int*);int flag=0;check(sym<Flag>(module,"_GetFlyFlag")(drawing,state,&flag)==0&&flag==2,"flight flag readback");
 // Original flight height depends on earlier frames. Draw the prefix so a
 // selected window has the same camera history as the complete flight.
 for(int step=0;step<start;++step){frame_rgb(module,drawing,state,width,height);memcpy(&index,data+1065199,4);check(index==step+1,"flight prefix advance");}
 NativeAvi avi;auto filename=ansi((folder+L"/flight.avi").c_str());check(GetFileAttributesA(filename.data())==INVALID_FILE_ATTRIBUTES,"flight AVI output exists");
 check(AVIFileOpenA(&avi.file,filename.data(),OF_CREATE|OF_WRITE,nullptr)==0,"flight AVI create");
 AVISTREAMINFOA info{};info.fccType=streamtypeVIDEO;info.fccHandler=mmioFOURCC('D','I','B',' ');info.dwScale=1;info.dwRate=fps;info.dwSuggestedBufferSize=stride*height;info.dwQuality=DWORD(-1);SetRect(&info.rcFrame,0,0,width,height);
 check(AVIFileCreateStreamA(avi.file,&avi.stream,&info)==0,"flight AVI stream");
 BITMAPINFOHEADER bitmap{};bitmap.biSize=sizeof(bitmap);bitmap.biWidth=width;bitmap.biHeight=height;bitmap.biPlanes=1;bitmap.biBitCount=24;bitmap.biCompression=BI_RGB;bitmap.biSizeImage=stride*height;
 check(AVIStreamSetFormat(avi.stream,0,&bitmap,sizeof(bitmap))==0,"flight AVI format");
 std::vector<unsigned char> rgb,bgr(size_t(stride)*height),top(size_t(width)*height*3);
 for(int frame=0;frame<frames;++frame){
  int current=0;memcpy(&current,data+1065199,4);check(current==start+frame,"flight initial step");rgb=frame_rgb(module,drawing,state,width,height);
  memcpy(&index,data+1065199,4);check(index==(current+1)%count,"one original step per frame");
  std::fill(bgr.begin(),bgr.end(),0);for(int y=0;y<height;++y){memcpy(top.data()+size_t(y)*width*3,rgb.data()+size_t(height-1-y)*width*3,size_t(width)*3);for(int x=0;x<width;++x)for(int band=0;band<3;++band)bgr[size_t(y)*stride+3*x+band]=rgb[(size_t(y)*width+x)*3+2-band];}
  long samples=0,bytes=0;check(AVIStreamWrite(avi.stream,frame,1,bgr.data(),long(bgr.size()),AVIIF_KEYFRAME,&samples,&bytes)==0&&samples==1&&bytes==long(bgr.size()),"flight frame write");
  float eye[3]{};check(sym<GetEye>(module,"_GetEyeParam")(drawing,state,eye,eye+1,eye+2)==0,"flight actual eye");for(float value:eye)check(std::isfinite(value),"flight finite effective eye");
  printf("{\"event\":\"flight_frame\",\"index\":%d,\"route_index\":%d,\"next_route_index\":%d,\"eye\":[%.9g,%.9g,%.9g],\"rgb_sha256\":\"%s\"}\n",frame,current,index,eye[0],eye[1],eye[2],flight_pixel_sha(top).c_str());fflush(stdout);
 }
 avi.close();write_frame(folder+L"/scene.tiles",width,height,rgb);auto final_camera=read_camera(module,drawing,state);camera_event(final_camera,"flight_final_camera");check(sym<SetDisplay>(module,"_SetFlyFlag")(drawing,state,0)==0,"flight stop");
 printf("{\"event\":\"flight_written\",\"generated_frames\":%d,\"frame_start\":%d,\"frames\":%d,\"fps\":%d,\"width\":%d,\"height\":%d,\"speed\":%.9g,\"speed_range\":[%.9g,%.9g]}\n",count,start,frames,fps,width,height,actual_speed,limits.first,limits.second);fflush(stdout);
 return final_camera;
}
void inspect_flight(const std::wstring& folder){
 auto path=ansi((folder+L"/flight.avi").c_str());if(GetFileAttributesA(path.data())==INVALID_FILE_ATTRIBUTES)return;
 NativeAvi avi;check(AVIFileOpenA(&avi.file,path.data(),OF_READ|OF_SHARE_DENY_WRITE,nullptr)==0,"flight AVI reopen");check(AVIFileGetStream(avi.file,&avi.stream,streamtypeVIDEO,0)==0,"flight AVI video stream");
 AVISTREAMINFOA info{};check(AVIStreamInfoA(avi.stream,&info,sizeof(info))==0&&info.fccType==streamtypeVIDEO&&info.dwScale==1&&info.dwRate>=1&&info.dwRate<=60,"flight AVI timing");
 long size=0;check(AVIStreamReadFormat(avi.stream,0,nullptr,&size)==0&&size==sizeof(BITMAPINFOHEADER),"flight AVI format size");BITMAPINFOHEADER format{};check(AVIStreamReadFormat(avi.stream,0,&format,&size)==0&&size==sizeof(format)&&format.biSize==sizeof(format)&&format.biPlanes==1&&format.biBitCount==24&&format.biCompression==BI_RGB&&format.biClrUsed==0,"flight AVI RGB profile");
 int width=format.biWidth,height=format.biHeight;check(width>=32&&width<=2048&&height>=32&&height<=2048,"flight AVI dimensions");long start=AVIStreamStart(avi.stream),frames=AVIStreamLength(avi.stream);check(start==0&&frames>=1&&frames<=1000&&info.dwLength==unsigned(frames),"flight AVI frame count");
 int stride=(width*3+3)&~3;check(format.biSizeImage==unsigned(stride*height)&&static_cast<unsigned long long>(stride)*height*frames<=1024ull*1024*1024-65536,"flight AVI byte profile");
 std::vector<unsigned char> bgr(size_t(stride)*height),top(size_t(width)*height*3);
 for(long frame=0;frame<frames;++frame){long bytes=0,samples=0;check(AVIStreamRead(avi.stream,frame,1,bgr.data(),long(bgr.size()),&bytes,&samples)==0&&samples==1&&bytes==long(bgr.size()),"flight AVI sample readback");for(int y=0;y<height;++y)for(int x=0;x<width;++x)for(int band=0;band<3;++band)top[(size_t(y)*width+x)*3+band]=bgr[size_t(height-1-y)*stride+x*3+2-band];printf("{\"event\":\"flight_frame_readback\",\"index\":%ld,\"rgb_sha256\":\"%s\"}\n",frame,flight_pixel_sha(top).c_str());fflush(stdout);}
 avi.close();printf("{\"event\":\"flight_readback\",\"frames\":%ld,\"fps\":%lu,\"width\":%d,\"height\":%d}\n",frames,info.dwRate,width,height);fflush(stdout);saved_file(folder,L"flight.avi");
}
