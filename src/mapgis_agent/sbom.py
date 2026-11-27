"""Inventory the installed distributions and pinned MapGIS redistribution components."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
import json
import os
from pathlib import Path
import re
import sys
import tomllib
from urllib.parse import quote
from uuid import uuid4

from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression
from packaging.utils import canonicalize_name


EXCLUDED_DISTRIBUTIONS = frozenset({'mapgis-local-agent', 'pefile'})
CORE_EXCLUDES = frozenset({'llvmlite', 'numba', 'pyflwdir', 'opencv-python-headless',
                           'opencv-python', 'opencv-contrib-python', 'opencv-contrib-python-headless'})
ROOT_ID = 'SPDXRef-MapGIS-Local-Agent'
REPOSITORY = 'https://github.com/Nolanlaw/mapgis-local-agent'
GENERATOR = 'Tool: mapgis-agent-sbom'

_LICENSE_HEADERS = {
    'bsd 3-clause license': 'BSD-3-Clause',
    'bsd 2-clause license': 'BSD-2-Clause',
    'mit license': 'MIT',
    'apache license, version 2.0': 'Apache-2.0',
    'apache software license 2.0': 'Apache-2.0',
}

_CLASSIFIERS = {
    'MIT License': 'MIT',
    'Mozilla Public License 2.0 (MPL 2.0)': 'MPL-2.0',
    'GNU Lesser General Public License v3 or later (LGPLv3+)': 'LGPL-3.0-or-later',
}

# The generic PSF wheel metadata does not identify the pywin32 project code's
# terms. Other included modules have separate notices under dist-info/licenses.
_EXACT_LICENSES = {
    ('pywin32', '312'): ('BSD-3-Clause', 'pywin32-312.dist-info/licenses/win32/License.txt; also see com/License.txt and component notices'),
}


@dataclass(frozen=True)
class LicenseEvidence:
    expression: str
    method: str
    source: str


def _expression(value: str) -> str | None:
    try:
        expression = canonicalize_license_expression(value)
    except InvalidLicenseExpression:
        return None
    # A custom reference would require an additional extracted-license record.
    return None if 'LicenseRef-' in expression else expression


def _license_signature(text: str) -> str | None:
    words = ' '.join(text.casefold().split())
    if 'the above bsd license applies to all code' in words and 'apache license, version 2.0' in words:
        return 'BSD-3-Clause AND (Apache-2.0 OR BSD-3-Clause)'

    # The primary terms precede the notices for bundled code in legacy files.
    primary = words[:2300]
    if 'redistribution and use in source and binary forms' in primary:
        if 'advertising materials' in primary:
            return 'BSD-4-Clause'
        if re.search(r'neither.{1,180}endorse or promote', primary):
            return 'BSD-3-Clause'
        if 'redistributions in binary form' in primary and 'provided' in primary:
            return 'BSD-2-Clause'
    if 'permission is hereby granted, free of charge' in primary and 'deal in the software without restriction' in primary:
        return 'MIT'
    if re.search(r'apache license\s+version 2\.0, january 2004', primary):
        return 'Apache-2.0'
    if 'mozilla public license version 2.0' in primary:
        return 'MPL-2.0'
    return None


def resolve_license(distribution: metadata.Distribution) -> LicenseEvidence:
    """Require an SPDX expression backed by metadata, licence text or an exact-version record."""
    info = distribution.metadata
    for field in ('License-Expression', 'License'):
        if value := info.get(field, '').strip():
            expression = _expression(value)
            if expression is None and field == 'License':
                expression = _LICENSE_HEADERS.get(value.casefold())
                if expression is None and len(value) > 140:
                    expression = _license_signature(value)
            if expression:
                return LicenseEvidence(expression, field, f'METADATA {field}')

    classifiers = [(label, _CLASSIFIERS[label]) for value in info.get_all('Classifier', [])
                   if value.startswith('License :: ')
                   and (label := value.split(' :: ')[-1]) in _CLASSIFIERS]
    if len({expression for _, expression in classifiers}) == 1:
        label, expression = classifiers[0]
        return LicenseEvidence(expression, 'Classifier', f'METADATA Classifier {label}')

    candidates = []
    primary_names = {'license', 'license.txt', 'license.md', 'copying', 'copying.txt'}
    for entry in distribution.files or ():
        parts = entry.parts
        if (len(parts) not in (2, 3) or not parts[0].endswith('.dist-info')
                or len(parts) == 3 and parts[1] != 'licenses' or entry.name.casefold() not in primary_names):
            continue
        path = distribution.locate_file(entry)
        if path.is_file():
            if expression := _license_signature(path.read_text(encoding='utf-8', errors='replace')):
                candidates.append((entry.as_posix(), expression))
    if len({expression for _, expression in candidates}) == 1:
        source, expression = sorted(candidates)[0]
        return LicenseEvidence(expression, 'License file', source)

    key = canonicalize_name(info['Name']), distribution.version
    if record := _EXACT_LICENSES.get(key):
        expression, source = record
        return LicenseEvidence(expression, 'Verified name and version', source)
    raise ValueError(f'Unresolved SPDX license for {key[0]}=={key[1]}')


def _package(spdx_id, name, version, license, evidence, *, location='NOASSERTION', purl=None):
    record = {'SPDXID': spdx_id, 'name': name, 'downloadLocation': location, 'filesAnalyzed': False,
              'licenseDeclared': license, 'licenseConcluded': license, 'copyrightText': 'NOASSERTION',
              'licenseComments': evidence}
    if version:
        record['versionInfo'] = version
    if purl:
        record['externalRefs'] = [{'referenceCategory': 'PACKAGE-MANAGER', 'referenceType': 'purl',
                                   'referenceLocator': purl}]
    return record


# id, name, version, licence, evidence, upstream. Native dependencies bundled
# in wheels are linked to the owning Python distribution below.
_COMPONENTS = (
    ('Vendor-mapgis2shp', 'mapgis2shp (vendored)', '2.6.0', 'Apache-2.0',
     'vendor/mapgis2shp/LICENSE and AGENT_PATCH.md', 'https://github.com/leecugb/mapgis2shp/tree/2acf993e81a1d8e1984f110aff6725672ac09149'),
    ('Vendor-geoscan', 'GeoScan', '6a5aa66192f17fd1d1aeb602f410aebff4d8bc92', 'MIT',
     'vendor/geoscan/LICENSE and SOURCE.json', 'https://github.com/keros68/geoscan/tree/6a5aa66192f17fd1d1aeb602f410aebff4d8bc92'),
    ('Vendor-stratigraphic-amenity', 'Stratigraphic Amenity', 'a389c0a299c4ee1007b2438d88d96093a3fbb994', 'MIT',
     'vendor/stratigraphic-amenity/LICENSE and SOURCE.json', 'https://github.com/EigenformAI/stratigraphic-amenity/tree/a389c0a299c4ee1007b2438d88d96093a3fbb994'),
    ('Vendor-cdt', 'CDT', '2.0.1', 'MPL-2.0 AND BSD-3-Clause AND (Apache-2.0 WITH LLVM-exception)',
     'vendor/cdt/SOURCE.json, CDT/LICENSE, CDT/LICENSE-predicates.txt and CDT/LICENSE-LLVM.txt', 'https://github.com/artem-ogre/CDT/tree/53eff288a4e77ad1c0afe1a10f60bd2b57f877b8'),
    ('Source-winhlp', 'winhlp', '0.4.3', 'GPL-2.0-only',
     'third_party/winhlp/LICENSE.md, README.md and NOTICE.md; separate help-indexing subprocess', 'https://github.com/bitplane/winhlp/tree/cb2661ef85693799641cf7ab258e7a4074732568'),
    ('Source-gdal-los', 'GDAL LOS (alg/los.cpp)', '3.13.0', 'MIT',
     'third_party/gdal_los/SOURCE.json and los.cpp', 'https://github.com/OSGeo/gdal/blob/130cb4675a0b42bd32acad56e57022cca44fc008/alg/los.cpp'),
    ('Source-hydrology', 'PyFlwDir hydrology provenance', '0.5.12', 'MIT',
     'third_party/hydrology/SOURCE.json; Numba and llvmlite have separate distribution records', 'https://github.com/Deltares/pyflwdir/tree/v0.5.12'),
    ('Vendor-ovobj-grids', 'ovobj-converter grid kernel', 'ef5a338f7c45d22c48c841bff8ae059cf75a1cd9', 'MIT',
     'src/mapgis_agent/_vendor/ovobj-grids.LICENSE and PATCH.md', 'https://github.com/good9527/ovobj-converter/tree/ef5a338f7c45d22c48c841bff8ae059cf75a1cd9'),
    ('Vendor-wellpathpy', 'wellpathpy (vendored)', '0.5.2', 'LGPL-3.0-only',
     'src/mapgis_agent/_vendor/wellpathpy/SOURCE.json and LICENSE.LGPL3', 'https://github.com/Zabamund/wellpathpy/tree/ccbb7cd803485a74d2de83ef4dd5912bb7f9c2bd'),
    ('Runtime-CPython', 'CPython (portable build)', '3.12.16', 'Python-2.0',
     'Pinned portable runtime in scripts/package_share.py, with the Python upstream LICENSE', 'https://github.com/astral-sh/python-build-standalone/releases/tag/20261026'),
    ('Runtime-Ghostscript', 'Ghostscript', '10.08.0', 'AGPL-3.0-or-later',
     'Artifex LICENSE and pinned source in scripts/package_share.py; separate CLI subprocess', 'https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/tag/gs10080'),
    ('Runtime-libvips', 'libvips', '8.18.7', 'LGPL-3.0-or-later',
     'packaging/native-sources.json and packaging/LIBVIPS-NOTICES.md', 'https://github.com/libvips/libvips/releases/tag/v8.18.7'),
    ('Runtime-GEOS-3.14.1', 'GEOS', '3.14.1', 'LGPL-2.1-or-later',
     'GEOS source pinned in scripts/package_share.py; Pyogrio and Shapely wheel readback', 'https://github.com/libgeos/geos/releases/tag/3.14.1'),
    ('Runtime-GEOS-3.13.0', 'GEOS', '3.13.0', 'LGPL-2.1-or-later',
     'Rasterio 1.4.4 wheel readback and GEOS source licence', 'https://github.com/libgeos/geos/releases/tag/3.13.0'),
    ('Runtime-GDAL-Rasterio', 'GDAL (Rasterio)', '3.10.3', 'MIT',
     'Rasterio 1.4.4 wheel readback; separate rasterio.libs DLLs', 'https://github.com/OSGeo/gdal/tree/v3.10.3'),
    ('Runtime-GDAL-Pyogrio', 'GDAL (Pyogrio)', '3.12.4', 'MIT',
     'Pyogrio 0.13.0 wheel readback; separate pyogrio.libs DLLs', 'https://github.com/OSGeo/gdal/tree/v3.12.4'),
    ('Runtime-OpenCV', 'OpenCV', '4.13.0', 'Apache-2.0',
     'cv2/LICENSE-3RD-PARTY.txt in opencv-python-headless 4.13.0.92', 'https://github.com/opencv/opencv/tree/4.13.0'),
    ('Runtime-FFmpeg', 'FFmpeg (OpenCV wheel)', None, 'LGPL-2.1-or-later',
     'cv2/LICENSE-3RD-PARTY.txt names the bundled library and LGPL 2.1 terms', 'https://ffmpeg.org/legal.html'),
)

_WHEEL_COMPONENTS = {
    'Runtime-libvips': (('pyvips-binary', '8.18.7'),),
    'Runtime-GEOS-3.14.1': (('pyogrio', '0.13.0'), ('shapely', '2.2.0')),
    'Runtime-GEOS-3.13.0': (('rasterio', '1.4.4'),),
    'Runtime-GDAL-Rasterio': (('rasterio', '1.4.4'),),
    'Runtime-GDAL-Pyogrio': (('pyogrio', '0.13.0'),),
    'Runtime-OpenCV': (('opencv-python-headless', '4.13.0.92'),),
    'Runtime-FFmpeg': (('opencv-python-headless', '4.13.0.92'),),
}


def _timestamp():
    epoch = os.environ.get('SOURCE_DATE_EPOCH')
    now = datetime.fromtimestamp(int(epoch), timezone.utc) if epoch is not None else datetime.now(timezone.utc)
    return now.isoformat(timespec='seconds').replace('+00:00', 'Z')


def generate_sbom(root: Path, *, distributions: Iterable[metadata.Distribution] | None = None,
                  profile: str = 'full', companion: dict | None = None) -> tuple[dict, dict]:
    """Return an SPDX 2.3 document and a per-distribution evidence summary."""
    if profile not in {'full', 'core'}:
        raise ValueError('profile must be full or core')
    excluded = EXCLUDED_DISTRIBUTIONS | (CORE_EXCLUDES if profile == 'core' else set())
    project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    version = project['version']
    packages = [_package(ROOT_ID, project['name'], version, 'Apache-2.0', 'LICENSE and NOTICE.md',
                         location=REPOSITORY, purl=f'pkg:pypi/{project["name"]}@{quote(version)}')]
    relationships = []
    evidence = []
    unresolved = []
    installed = {}
    candidates = metadata.distributions() if distributions is None else distributions
    for item in sorted(candidates, key=lambda dist: canonicalize_name(dist.metadata['Name'])):
        name = canonicalize_name(item.metadata['Name'])
        if name in excluded:
            continue
        if name in installed:
            raise ValueError(f'Duplicate installed distribution: {name}')
        installed[name] = item.version
        try:
            found = resolve_license(item)
        except ValueError as exc:
            unresolved.append(str(exc))
            continue
        identifier = 'SPDXRef-PyPI-' + name
        packages.append(_package(identifier, item.metadata['Name'], item.version, found.expression,
                                 f'{found.method}: {found.source}', purl=f'pkg:pypi/{name}@{quote(item.version)}'))
        relationships.append(_relationship(ROOT_ID, 'DEPENDS_ON', identifier))
        evidence.append({'name': name, 'version': item.version, 'license': found.expression,
                         'method': found.method, 'evidence': found.source})
    if unresolved:
        raise ValueError('\n'.join(unresolved))

    for identifier, name, component_version, license, source, url in _COMPONENTS:
        owners = _WHEEL_COMPONENTS.get(identifier)
        parents = ([f'SPDXRef-PyPI-{key}' for key, exact in owners if installed.get(key) == exact]
                   if owners is not None else [ROOT_ID])
        if not parents:
            continue
        component_id = 'SPDXRef-' + identifier
        packages.append(_package(component_id, name, component_version, license, source, location=url))
        relationships.extend(_relationship(parent, 'CONTAINS', component_id) for parent in parents)

    created = _timestamp()
    document = {'spdxVersion': 'SPDX-2.3', 'dataLicense': 'CC0-1.0', 'SPDXID': 'SPDXRef-DOCUMENT',
                'name': f'MapGIS Local Agent {version} {profile}', 'documentNamespace': f'{REPOSITORY}/spdx/{version}/{uuid4()}',
                'creationInfo': {'created': created, 'creators': [GENERATOR]}, 'documentDescribes': [ROOT_ID],
                'packages': packages, 'relationships': relationships}
    if companion is not None:
        document['annotations'] = [{'annotationDate': created, 'annotationType': 'OTHER', 'annotator': GENERATOR,
                                    'comment': json.dumps(companion, ensure_ascii=False, sort_keys=True)}]
    validate_sbom(document)
    summary = {'profile': profile, 'inventory_python_version': sys.version.split()[0],
               'installed_distribution_count': len(evidence), 'package_count': len(packages),
               'unknown_license_count': 0, 'excluded_distributions': sorted(excluded),
               'resolution_methods': dict(sorted(Counter(item['method'] for item in evidence).items())),
               'distributions': evidence}
    return document, summary


def _relationship(parent, kind, child):
    return {'spdxElementId': parent, 'relationshipType': kind, 'relatedSpdxElement': child}


def validate_sbom(document: dict) -> None:
    """Fail on absent, unresolved or non-SPDX package licences and broken package references."""
    if document.get('spdxVersion') != 'SPDX-2.3' or document.get('dataLicense') != 'CC0-1.0':
        raise ValueError('Expected SPDX 2.3 and CC0-1.0 document data licence')
    ids = set()
    for package in document.get('packages', []):
        identity = package['SPDXID']
        if identity in ids or not re.fullmatch(r'SPDXRef-[A-Za-z0-9.-]+', identity):
            raise ValueError(f'Invalid or duplicate package ID: {identity}')
        ids.add(identity)
        for field in ('licenseDeclared', 'licenseConcluded'):
            if not _expression(package.get(field, '')):
                raise ValueError(f'Unresolved SPDX license: {package["name"]} {field}')
    if ROOT_ID not in ids or document.get('documentDescribes') != [ROOT_ID]:
        raise ValueError('Missing document root package reference')
    for relationship in document.get('relationships', []):
        if {relationship['spdxElementId'], relationship['relatedSpdxElement']} - ids:
            raise ValueError('SBOM relationship references an absent package')
