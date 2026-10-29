"""Record the just-built owned worker; never includes MapGIS binaries."""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
binary=root/'src/mapgis_agent/bin/MapgisNativeWorker.exe'
source=root/'native/MapgisNativeWorker.cpp'
manifest={'protocol':1,'architecture':'x86','sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
 'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
 'bas60_sha256':'680029d3ed7a04338a6799f1e16cab30d6e421d13e3d8c66bbeccb057a50ee24',
 'legacy_dep_compatibility':True,'operations':['inspect','resave','point.move','feature.delete','plot','workspace','image.to_msi','image.to_tiff','project.create','project.inspect','project.plot'],
 'headers':{'PrivateDesktop.h':hashlib.sha256((root/'native/PrivateDesktop.h').read_bytes()).hexdigest()}}
(binary.parent/'native-worker.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf8')
