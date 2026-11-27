"""Source entry also works from a moved package; no editable .pth file needed."""
from pathlib import Path
import os
import sys

# Each chat owns a lightweight stdio process. Avoid reserving a full CPU-sized
# BLAS/OpenMP pool in every idle copy; explicit caller settings take precedence.
for variable in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(variable, '1')

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from mapgis_agent import _mcp_main

if __name__ == "__main__":
    _mcp_main(default_root=root)
