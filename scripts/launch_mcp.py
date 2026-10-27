"""Source entry also works from a moved package; no editable .pth file needed."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from mapgis_agent.server import main

if __name__ == "__main__":
    if "--root" not in sys.argv:
        sys.argv[1:1] = ["--root", str(root)]
    main()
