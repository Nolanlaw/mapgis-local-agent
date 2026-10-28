@echo off
rem Build only this new x86 legacy host; no system DEP settings are changed.
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars32.bat" >nul
if errorlevel 1 exit /b %errorlevel%
cl /nologo /O2 /EHsc /MT /utf-8 /Fe:src\mapgis_agent\bin\MapgisNativeWorker.exe /Fo:work\native-interop\MapgisNativeWorker.obj native\MapgisNativeWorker.cpp /link /NXCOMPAT:NO
exit /b %errorlevel%
