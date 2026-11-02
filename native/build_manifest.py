"""Record the just-built owned worker; never includes MapGIS binaries."""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
binary=root/'src/mapgis_agent/bin/MapgisNativeWorker.exe'
source=root/'native/MapgisNativeWorker.cpp'
manifest={'protocol':1,'architecture':'x86','sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
 'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
 'bas60_sha256':'680029d3ed7a04338a6799f1e16cab30d6e421d13e3d8c66bbeccb057a50ee24',
 'legacy_dep_compatibility':True,'operations':['inspect','resave','point.move','line.replace','arc.replace','node.move','feature.delete','plot','plot.ps','workspace','image.to_msi','image.to_tiff','image.from_raw','image.to_raw','project.create','project.inspect','project.edit','project.plot','project.plot.ps'],
 'headers':{name:hashlib.sha256((root/'native'/name).read_bytes()).hexdigest()
            for name in ('PrivateDesktop.h','NativeTerrain.h','NativeTin.h','NativeMsd.h','NativeCom.h','NativeSdk.h','NativeLegend.h','NativeScene.h','NativeCamera.h','NativeFlight.h','NativeAnnotations.h','NativeRegions.h','NativeTinScene.h','NativeTinLines.h','NativeTinFlight.h')}}
manifest['operations']+=['terrain.idw','terrain.inspect','terrain.tin','terrain.tin.inspect','terrain.tin.write']
manifest['operations']+=['msd.create','msd.inspect','msd.edit']
manifest['operations']+=['com.subset','com.append']
manifest['operations']+=['sdk.typed_flow']
manifest['operations']+=['com.project.params.native.inspect','com.project.params.native.edit']
manifest['operations']+=['com.legend.create','com.legend.inspect']
manifest['operations']+=['point.radius','point.style','point.text']
manifest['operations']+=['scene.create','scene.render','scene.flight','scene.inspect']
manifest['operations']+=['tin.scene.render','tin.scene.inspect']
cdt=root/'vendor/cdt/SOURCE.json'
manifest['cdt']={'version':'2.0.1','commit':'53eff288a4e77ad1c0afe1a10f60bd2b57f877b8','source_manifest_sha256':hashlib.sha256(cdt.read_bytes()).hexdigest()}
(binary.parent/'native-worker.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf8')
