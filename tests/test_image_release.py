"""Release preparation must not silently build a different application version."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'prepare_image_build.py'


@pytest.mark.parametrize(('version', 'base', 'success'), [
    ('v0.2.4', 'python@sha256:' + 'a' * 64, True),
    ('latest', 'python@sha256:' + 'a' * 64, False),
    ('v0.2.5', 'python@sha256:' + 'a' * 64, False),
    ('v0.2.4', 'python:latest', False),
])
def test_fixed_source_and_base(tmp_path, version, base, success):
    source = tmp_path / 'release-source'
    source.mkdir()
    (source / 'pyproject.toml').write_text('[project]\nversion="0.2.4"\n')
    (source / 'Dockerfile').write_text('FROM python:3.11.16-slim-bookworm\nRUN true\n')
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '-qm', 'test'], check=True)
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path,
                            env={**os.environ, 'VERSION': version, 'BASE_DIGEST': base}, capture_output=True)
    assert (result.returncode == 0) is success
    if success:
        manifest = json.loads((tmp_path / 'image-output/image-manifest.json').read_text())
        assert manifest['base_image'] == base and not manifest['tested']
        assert (source / 'Dockerfile').read_text().startswith('FROM python:3.11.16')
        assert (tmp_path / 'image-output/Dockerfile.pinned').read_text().startswith('FROM python@sha256:')
