"""Generate or check the project's SPDX 2.3 distribution inventory."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from mapgis_agent.sbom import generate_sbom, validate_sbom


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'packaging/sbom.spdx.json')
    parser.add_argument('--summary', type=Path)
    parser.add_argument('--check', action='store_true', help='Validate the existing --output without modifying files')
    args = parser.parse_args()
    if args.check:
        if args.summary is not None:
            parser.error('--summary is only available when generating an SBOM')
        document = json.loads(args.output.read_text(encoding='utf-8'))
        validate_sbom(document)
        result = {'package_count': len(document['packages']), 'unknown_license_count': 0}
    else:
        document, summary = generate_sbom(ROOT)
        for path, content in ((args.output, document), (args.summary, summary)):
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        result = {key: value for key, value in summary.items() if key != 'distributions'}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
