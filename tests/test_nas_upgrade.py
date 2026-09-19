"""Exercise upgrade failure/rollback with a fake Docker CLI and real temporary files."""
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
IMAGE = 'ghcr.io/n0tevii/second-eye@sha256:' + 'a' * 64

FAKE_DOCKER = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
state_file = pathlib.Path(os.environ['FAKE_STATE'])
state = json.loads(state_file.read_text())
with open(os.environ['FAKE_CALLS'], 'a') as log:
    log.write(json.dumps(args) + '\n')
if args[0] == 'compose':
    if 'version' in args:
        sys.exit(0)
    if 'config' in args:
        if '--services' in args: print('second-eye')
        if '--images' in args: print('second-eye:v0.2.3')
        sys.exit(0)
    if 'up' in args:
        config = pathlib.Path(args[args.index('--file')+1]).read_text()
        new = 'ghcr.io/' in config
        if os.environ.get('FAKE_FAIL') == 'rollback': sys.exit(1)
        if new and os.environ.get('FAKE_FAIL') == 'create': sys.exit(1)
        state['image'] = 'sha256:new' if new else 'sha256:old'
        state['running'] = 'true'
elif args[0] == 'inspect':
    fmt = args[args.index('--format')+1]
    if 'Labels' in fmt: print('second-eye')
    elif 'Running' in fmt: print(state['running'])
    elif '.Image' in fmt: print(state['image'])
    else: print('container-id')
elif args[:2] == ['image', 'inspect']:
    fmt = args[args.index('--format')+1]
    if 'Architecture' in fmt: print('linux/amd64')
    elif 'version' in fmt: print('v0.2.4')
    else: print('sha256:new' if 'ghcr.io' in args[-1] else 'sha256:old')
elif args[0] == 'pull':
    if os.environ.get('FAKE_FAIL') == 'pull': sys.exit(1)
elif args[0] in ('stop', 'start'):
    state['running'] = 'false' if args[0] == 'stop' else 'true'
elif args[0] == 'run' and args[-1] == 'pause':
    root = pathlib.Path(os.environ['FAKE_ROOT'])
    (root/'data/goodprice.db').write_text('paused-and-migrated')
    print('[1]')
elif args[0] == 'exec':
    if any('healthz' in a for a in args) and os.environ.get('FAKE_FAIL') == 'health': sys.exit(1)
    if 'resume' in args and os.environ.get('FAKE_FAIL') == 'resume': sys.exit(1)
state_file.write_text(json.dumps(state))
'''


@pytest.fixture
def upgrade_fixture(tmp_path):
    root = tmp_path / 'second-eye'
    tools = root / 'tools'
    tools.mkdir(parents=True)
    (root / 'data').mkdir()
    (root / 'data/goodprice.db').write_text('original-database')
    (root / '.env').write_text('ADMIN_PASSWORD=test-secret-never-print\n')
    (root / 'compose.yml').write_text('services:\n  second-eye:\n    image: second-eye:v0.2.3\n')
    (tools / 'compose.image.yml').write_bytes((REPO/'deploy/compose.image.yml').read_bytes())
    (tools / 'upgrade_db.py').write_bytes((REPO/'scripts/upgrade_db.py').read_bytes())
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, content in [('docker', '#!' + sys.executable + '\n' + FAKE_DOCKER), ('sleep', '#!/bin/sh\nexit 0\n')]:
        path = bin_dir / name
        path.write_text(content)
        path.chmod(0o755)
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'image': 'sha256:old', 'running': 'true'}))
    calls = tmp_path / 'calls.jsonl'
    env = {**os.environ, 'PATH': str(bin_dir)+':'+os.environ['PATH'],
           'FAKE_STATE': str(state), 'FAKE_CALLS': str(calls), 'FAKE_ROOT': str(root)}
    return root, state, calls, env


def run_upgrade(fixture, mode='upgrade', failure=''):
    root, state, calls, env = fixture
    result = subprocess.run(['sh', str(REPO/'scripts/nas_upgrade.sh'), mode, str(root),
                             'compose.yml', 'v0.2.4', IMAGE],
                            env={**env, 'FAKE_FAIL': failure}, capture_output=True, text=True)
    assert 'test-secret-never-print' not in result.stdout + result.stderr
    return result


def test_preflight_is_read_only(upgrade_fixture):
    root, _, calls, _ = upgrade_fixture
    assert run_upgrade(upgrade_fixture, 'check').returncode == 0
    assert not (root / 'backups').exists()
    operations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert all(op[0] in ('compose', 'inspect', 'image') for op in operations)


def test_upgrade_preserves_backup_and_resumes_tasks(upgrade_fixture):
    root, state, calls, _ = upgrade_fixture
    result = run_upgrade(upgrade_fixture)
    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text())['image'] == 'sha256:new'
    backup = next((root / 'backups').iterdir())
    assert (backup / 'data.tar').exists() and (backup / 'env.before').exists()
    assert (backup / 'enabled-tasks.json').read_text().strip() == '[1]'
    assert 'resume' in calls.read_text()
    assert IMAGE in (root / 'compose.yml').read_text()
    assert 'NOT_VALIDATED' in result.stdout
    assert not (root / '.upgrade-lock').exists()


@pytest.mark.parametrize('failure', ['create', 'health', 'resume'])
def test_failure_restores_matching_data_and_old_image(upgrade_fixture, failure):
    root, state, _, _ = upgrade_fixture
    assert run_upgrade(upgrade_fixture, failure=failure).returncode != 0
    assert json.loads(state.read_text()) == {'image': 'sha256:old', 'running': 'true'}
    assert (root / 'data/goodprice.db').read_text() == 'original-database'
    assert 'second-eye:v0.2.3' in (root / 'compose.yml').read_text()
    backup = next((root / 'backups').iterdir())
    assert (backup / 'failed-data/goodprice.db').read_text() == 'paused-and-migrated'


def test_pull_failure_does_not_stop_existing_container(upgrade_fixture):
    root, state, calls, _ = upgrade_fixture
    assert run_upgrade(upgrade_fixture, failure='pull').returncode != 0
    assert json.loads(state.read_text())['running'] == 'true'
    assert not (root / 'backups').exists()
    assert not any(json.loads(line)[0] == 'stop' for line in calls.read_text().splitlines())


def test_existing_lock_refuses_upgrade(upgrade_fixture):
    root, _, calls, _ = upgrade_fixture
    (root / '.upgrade-lock').mkdir()
    assert run_upgrade(upgrade_fixture).returncode != 0
    assert not any(json.loads(line)[0] == 'pull' for line in calls.read_text().splitlines())


def test_backup_failure_restarts_unchanged_container(upgrade_fixture):
    root, state, calls, env = upgrade_fixture
    fake_tar = Path(env['PATH'].split(':')[0]) / 'tar'
    fake_tar.write_text('#!/bin/sh\nexit 1\n')
    fake_tar.chmod(0o755)
    assert run_upgrade(upgrade_fixture).returncode != 0
    assert json.loads(state.read_text()) == {'image': 'sha256:old', 'running': 'true'}
    assert (root / 'data/goodprice.db').read_text() == 'original-database'
    assert 'pause' not in calls.read_text()


def test_failed_rollback_keeps_lock_and_reports_incomplete(upgrade_fixture):
    root, state, _, _ = upgrade_fixture
    result = run_upgrade(upgrade_fixture, failure='rollback')
    assert result.returncode != 0
    assert 'ROLLBACK INCOMPLETE' in result.stderr
    assert json.loads(state.read_text())['running'] == 'false'
    assert (root / '.upgrade-lock').is_dir()
    assert (root / 'data/goodprice.db').read_text() == 'original-database'


def test_pause_resume_only_originally_enabled_tasks(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'db.sqlite'
    connect = sqlite3.connect
    with connect(path) as db:
        db.execute('CREATE TABLE watch_tasks(id INTEGER PRIMARY KEY, enabled BOOLEAN)')
        db.executemany('INSERT INTO watch_tasks VALUES (?, ?)', [(1, True), (2, False)])
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: connect(path))
    monkeypatch.setattr(sys, 'argv', ['upgrade_db.py', 'pause'])
    runpy.run_path(str(REPO/'scripts/upgrade_db.py'))
    saved = capsys.readouterr().out.strip()
    assert json.loads(saved) == [1]
    with connect(path) as db:
        assert db.execute('SELECT SUM(enabled) FROM watch_tasks').fetchone()[0] == 0
    monkeypatch.setattr(sys, 'argv', ['upgrade_db.py', 'resume', saved])
    runpy.run_path(str(REPO/'scripts/upgrade_db.py'))
    with connect(path) as db:
        assert db.execute('SELECT id, enabled FROM watch_tasks ORDER BY id').fetchall() == [(1, 1), (2, 0)]
