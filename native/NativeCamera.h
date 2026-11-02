// Saved scalar data belongs only to this pinned, newly created scene instance.
#pragma once
struct NativeCameraPose {float value[12]{};float world_eye[3]{};};
using CameraPairGet=long(__cdecl*)(void*,void*,float*,float*);
using CameraPairSet=long(__cdecl*)(void*,void*,float,float);
NativeCameraPose read_camera(HMODULE module,void* drawing,void* state){
 const int offsets[]={1065047,1065051,1065095,1065099,1065103,1065123,1065127,1065131,361,365,369,373};
 NativeCameraPose pose;auto data=static_cast<unsigned char*>(state);for(int i=0;i<12;++i){memcpy(pose.value+i,data+offsets[i],4);check(std::isfinite(pose.value[i]),"scene finite saved camera");}
 check(sym<GetEye>(module,"_GetEyeParam")(drawing,state,pose.world_eye,pose.world_eye+1,pose.world_eye+2)==0,"scene saved camera world eye");return pose;
}
void camera_event(const NativeCameraPose& pose,const char* event){
 auto v=pose.value;printf("{\"event\":\"%s\",\"pose\":{\"profile\":\"newtin67.camera.v1\",\"basis\":[%.9g,%.9g,%.9g,%.9g,%.9g],\"eye_model\":[%.9g,%.9g,%.9g],\"move_degrees\":%.9g,\"view_degrees\":%.9g,\"fov_degrees\":%.9g,\"tilt_degrees\":%.9g},\"world_eye\":[%.9g,%.9g,%.9g]}\n",event,v[0],v[1],v[2],v[3],v[4],v[5],v[6],v[7],v[8],v[9],v[10],v[11],pose.world_eye[0],pose.world_eye[1],pose.world_eye[2]);fflush(stdout);
}
bool restore_camera(const std::wstring& folder,HMODULE module,void* drawing,void* state,int display){
 auto filename=ansi((folder+L"/camera.txt").c_str());if(GetFileAttributesA(filename.data())==INVALID_FILE_ATTRIBUTES)return false;
 check(display==1,"saved model camera requires walk mode");std::ifstream input(filename.data());input.imbue(std::locale::classic());int version=0;NativeCameraPose pose;input>>version;for(float& value:pose.value)input>>value;std::string extra;check(input&&!(input>>extra)&&version==1,"saved camera input");
 for(float value:pose.value)check(std::isfinite(value),"finite saved camera input");auto actual=read_camera(module,drawing,state);
 for(int i=0;i<5;++i)check(actual.value[i]==pose.value[i]&&(i<2||pose.value[i]>0),"saved camera model basis changed");
 auto v=pose.value;auto ranges=static_cast<float*>(drawing);check(v[5]>=ranges[7]&&v[5]<=ranges[8]&&v[6]>=ranges[10]&&v[6]<=ranges[11],"saved camera horizontal range");
 check(v[8]>=0&&v[8]<=360&&v[9]>=0&&v[9]<=360&&v[10]>=30&&v[10]<=120&&v[11]>=0&&v[11]<=90,"saved camera angle bounds");
 // World XY setters take float32 and lose subpixel positions at large source
 // offsets. Restore only the three verified owned model-coordinate scalars.
 auto data=static_cast<unsigned char*>(state);memcpy(data+1065123,v+5,12);
 check(sym<CameraPairSet>(module,"_SetFly_ViewDirAngle")(drawing,state,v[8],v[9])==0,"saved camera directions");
 check(sym<CameraPairSet>(module,"_SetViewFov_TiltAngle")(drawing,state,v[10],v[11])==0,"saved camera projection");return true;
}
