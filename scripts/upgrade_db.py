"""Pause/resume only previously enabled tasks during a stopped-container upgrade.

Executed with the fixed image's Python, mounting only the project's data directory.
The output contains task IDs only, never credentials or listing contents.
"""
import json
import sqlite3
import sys

mode = sys.argv[1]
assert mode in {'pause', 'resume'}
with sqlite3.connect('file:/app/data/goodprice.db?mode=rw', uri=True) as db:
    db.execute('BEGIN IMMEDIATE')
    if mode == 'pause':
        enabled = [row[0] for row in db.execute('SELECT id FROM watch_tasks WHERE enabled=1')]
        db.execute('UPDATE watch_tasks SET enabled=0 WHERE enabled=1')
    else:
        enabled = json.loads(sys.argv[2])
        assert isinstance(enabled, list) and all(type(i) is int and i > 0 for i in enabled)
        db.executemany('UPDATE watch_tasks SET enabled=1 WHERE id=?', [(i,) for i in enabled])
if mode == 'pause':
    print(json.dumps(enabled))
