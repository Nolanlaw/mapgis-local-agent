"""Exercise staging in an isolated copy, including alias collision and replay."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction workflow')
@pytest.mark.parametrize('shell', ['powershell.exe', 'pwsh.exe'])
def test_staging_preserves_inputs_and_refuses_alias_retarget_and_replay(tmp_path, shell):
    if not shutil.which(shell):
        pytest.skip(f'{shell} not installed')
    package = tmp_path / 'package'
    (package / 'scripts').mkdir(parents=True)
    shutil.copyfile(Path(__file__).parents[1] / 'scripts/Prepare-NativeOutput.ps1',
                    package / 'scripts/Prepare-NativeOutput.ps1')
    source = tmp_path / 'source'
    source.mkdir()
    inputs = {'test.MPJ': b'opaque project bytes', 'layer.WP': b'opaque native bytes'}
    for name, value in inputs.items():
        (source / name).write_bytes(value)
    manifest = tmp_path / 'input.json'
    manifest.write_text(json.dumps({'project': str(source / 'test.MPJ'), 'files': ['layer.WP']}), encoding='utf-8')
    alias = Path(tmp_path.anchor) / ('mg67-' + uuid.uuid4().hex[:12])
    script = package / 'scripts/Prepare-NativeOutput.ps1'

    def invoke(request):
        return subprocess.run([shell, '-NoProfile', '-File', str(script),
            '-SourceManifest', str(manifest), '-RequestId', request, '-ShortAlias', str(alias)],
            capture_output=True, timeout=30)

    try:
        first = invoke('first')
        assert first.returncode == 0, first.stderr.decode(errors='replace')
        receipt = json.loads((package / 'evidence/native-output/first.json').read_text(encoding='utf-8'))
        assert receipt['copy_hashes_verified'] is True
        assert alias.resolve() == (package / 'work/native-output/first').resolve()
        for entry in receipt['files']:
            value = inputs[entry['name']]
            assert (source / entry['name']).read_bytes() == value
            assert (alias / entry['name']).read_bytes() == value
            assert entry['sha256'] == hashlib.sha256(value).hexdigest()
        assert invoke('first').returncode != 0
        assert invoke('second').returncode != 0
        assert not (package / 'work/native-output/second').exists()
        assert alias.resolve() == (package / 'work/native-output/first').resolve()
    finally:
        # Remove only the exact test junction, never recurse into its target.
        if alias.is_junction():
            assert alias.resolve().is_relative_to(package.resolve())
            os.rmdir(alias)
