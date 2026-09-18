"""Real native form submission; HTTP clients do not apply Referrer-Policy."""

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from goodprice.main import build_app
from goodprice.services.settings_service import SettingsService


def test_browser_can_save_settings_without_leaking_external_referrer(
    base_settings, session_factory,
):
    with sync_playwright() as playwright:
        executable = os.environ.get("TEST_BROWSER_EXECUTABLE", playwright.chromium.executable_path)
        if not Path(executable).exists():
            pytest.skip("Install Playwright Chromium to run browser form regression")
        app = build_app(base_settings, session_factory, with_scheduler=False)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert server.started
                browser = playwright.chromium.launch(headless=True, executable_path=executable)
                try:
                    context = browser.new_context(http_credentials={
                        "username": base_settings.admin_username,
                        "password": base_settings.admin_password,
                    })
                    page = context.new_page()
                    page.goto(url + "/settings")
                    page.wait_for_load_state("networkidle")
                    page.locator('input[name="llm_model"]').fill("browser-test-model")
                    with page.expect_response(
                        lambda r: r.request.method == "POST" and r.url == url + "/settings"
                    ) as submitted:
                        page.get_by_role("button", name="保存设置", exact=True).click()
                    response = submitted.value
                    assert response.status == 303, {
                        "status": response.status,
                        "origin": response.request.headers.get("origin"),
                        "referer": response.request.headers.get("referer"),
                    }
                    page.wait_for_load_state("networkidle")
                    runtime = SettingsService(session_factory, base_settings).get()
                    assert runtime.llm_model == "browser-test-model"
                    assert page.locator('input[name="llm_model"]').input_value() == runtime.llm_model

                    # An outbound navigation must not reveal management-page URLs.
                    context.route("https://external.example/**", lambda r: r.fulfill(body="ok"))
                    page.evaluate("""() => {
                        const a = document.createElement('a');
                        a.href = 'https://external.example/';
                        a.textContent = 'external test';
                        document.body.appendChild(a);
                    }""")
                    with page.expect_request("https://external.example/") as outbound:
                        page.get_by_role("link", name="external test", exact=True).click()
                    assert "referer" not in outbound.value.headers
                finally:
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                assert not thread.is_alive()
