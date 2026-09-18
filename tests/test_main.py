import logging

from fastapi.testclient import TestClient
from logging.handlers import RotatingFileHandler

from goodprice.main import build_app


def test_build_app_health(base_settings, session_factory):
    app = build_app(settings=base_settings, session_factory=session_factory, with_scheduler=False)
    with TestClient(app) as client:
        response = client.get(
            "/api/stats",
            headers={"Authorization": "Basic dGVzdC1hZG1pbjp0ZXN0LXBhc3N3b3JkLTEyMzQ="},
        )
    assert response.status_code == 200
    assert response.json() == {"tasks": 0, "enabled_tasks": 0, "listings": 0, "notified": 0}


def test_build_app_requires_admin_password(base_settings, session_factory):
    insecure = base_settings.model_copy(update={"admin_password": ""})
    import pytest

    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        build_app(settings=insecure, session_factory=session_factory, with_scheduler=False)


def test_main_entry_importable():
    import goodprice.__main__  # noqa: F401


def test_logging_skips_file_handler_under_pytest():
    from goodprice.main import _setup_logging

    _setup_logging()
    handlers = logging.getLogger().handlers
    assert not any(isinstance(h, RotatingFileHandler) for h in handlers)


def test_redact_secrets_covers_notification_urls():
    from goodprice.security import redact_secrets

    text = (
        "https://sctapi.ftqq.com/SCT123.send "
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc "
        "https://open.feishu.cn/open-apis/bot/v2/hook/secret-id"
    )
    redacted = redact_secrets(text)
    assert "SCT123" not in redacted
    assert "key=abc" not in redacted
    assert "secret-id" not in redacted
