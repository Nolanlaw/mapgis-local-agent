"""Tool discovery must not retain a hydrology compiler in every idle server."""
import json
from pathlib import Path
import subprocess
import sys


def test_idle_tool_discovery_does_not_load_hydrology_compiler():
    source=Path(__file__).resolve().parents[1]/'src'
    script="""import sys,json,asyncio
sys.path.insert(0,sys.argv[1])
from mapgis_agent.server import build_server
import psutil
server=build_server(sys.argv[2])
tools=asyncio.run(server.list_tools())
print(json.dumps({'tools':len(tools),'heavy_loaded':any(n=='pyflwdir' or n=='numba' or n.startswith('llvmlite.binding') for n in sys.modules),'private_bytes':getattr(psutil.Process().memory_info(),'private',None)}))
"""
    import tempfile
    with tempfile.TemporaryDirectory() as workspace:
        reply=subprocess.run([sys.executable,'-s','-B','-X','utf8','-c',script,str(source),workspace],
            capture_output=True,text=True,encoding='utf8',timeout=45,check=True)
    result=json.loads(reply.stdout)
    assert result['tools']>=100
    assert not result['heavy_loaded'],result
    if result['private_bytes'] is not None:assert result['private_bytes']<500*1024**2,result
