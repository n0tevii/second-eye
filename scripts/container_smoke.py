"""Inside an isolated test container: no real account, model or notification calls."""
import os
import socket
import sys
import time

import httpx
from playwright.sync_api import sync_playwright
from goodprice import __version__

expected = sys.argv[1]
assert __version__ == expected.removeprefix('v')
assert os.environ['APP_VERSION'] == expected
with httpx.Client(base_url='http://127.0.0.1:8000', timeout=3) as client:
    for attempt in range(60):
        try:
            health = client.get('/healthz')
            if health.status_code == 200:
                break
        except httpx.TransportError:
            pass
        time.sleep(1)
    else:
        raise AssertionError('Application did not become healthy')
    assert health.json()['version'] == expected
    for path in ('/', '/settings', '/api/tasks'):
        response = client.get(path)
        assert response.status_code == 401
        assert 'no-store' in response.headers['cache-control']
    auth = ('ci-admin', 'ci-only-test-password-1234')
    assert client.get('/settings', auth=auth).status_code == 200
    assert client.post('/settings', auth=auth, headers={'Origin': 'https://untrusted.invalid'}).status_code == 403
for port in (5900, 5901, 6080):
    with socket.socket() as sock:
        assert sock.connect_ex(('127.0.0.1', port)) != 0, f'unexpected listener {port}'
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content('<h1>offline chromium test</h1>')
    assert page.locator('h1').inner_text() == 'offline chromium test'
    browser.close()
print(f'PASS {expected}: installed code, health version, auth, CSRF, noVNC off, Chromium')
