"""Record the exact source/base used to build a fixed release, without editing its tag."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

source = Path('release-source')
out = Path('image-output')
out.mkdir(exist_ok=True)
version = os.environ['VERSION']
assert re.fullmatch(r'v\d+\.\d+\.\d+', version)
if '--published' in sys.argv:
    manifest = json.loads((out / 'image-manifest.json').read_text())
    image = (out / 'image-reference.txt').read_text().strip()
    assert re.fullmatch(r'ghcr\.io/n0tevii/second-eye@sha256:[a-f0-9]{64}', image)
    manifest.update(image=image, tested=True)
else:
    package = tomllib.loads((source / 'pyproject.toml').read_text())
    assert package['project']['version'] == version[1:]
    base = os.environ['BASE_DIGEST']
    assert re.fullmatch(r'(?:docker.io/library/)?python@sha256:[a-f0-9]{64}', base)
    dockerfile = (source / 'Dockerfile').read_text().splitlines(keepends=True)
    assert dockerfile[0].startswith('FROM python:')
    dockerfile[0] = f'FROM {base}\n'
    (out / 'Dockerfile.pinned').write_text(''.join(dockerfile))
    manifest = {
        'version': version,
        'source_commit': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
        'platform': 'linux/amd64',
        'base_image': base,
        'tested': False,
    }
(out / 'image-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps(manifest))
