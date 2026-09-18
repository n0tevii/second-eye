import base64
import binascii
import re
import secrets
from urllib.parse import urlsplit

from starlette.responses import JSONResponse, Response


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:key|token|secret|sendkey|access_token)=)[^&#\s]+", re.IGNORECASE
)


def redact_secrets(value: object) -> str:
    """Remove common URL/query credentials before persisting or logging errors."""
    text = str(value)
    text = re.sub(r"(https?://)([^/@\s:]+):([^/@\s]+)@", r"\1***:***@", text)
    text = SENSITIVE_QUERY_RE.sub(r"\1***", text)
    text = re.sub(r"(sctapi\.ftqq\.com/)[^/'\"\s?]+(\.send)", r"\1***\2", text)
    return re.sub(r"(/(?:bot/v2/)?hook/)[^/'\"\s?#]+", r"\1***", text)


class AdminSecurityMiddleware:
    """Protect every management route with Basic auth and same-origin writes."""

    def __init__(self, app, username: str, password: str):
        if not username or not password:
            raise RuntimeError("ADMIN_USERNAME 和 ADMIN_PASSWORD 必须配置后才能启动")
        self.app = app
        self.username = username
        self.password = password

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        path = scope.get("path", "")
        if path != "/healthz" and not self._authorized(headers.get(b"authorization", b"")):
            response = Response(
                "需要管理账号认证",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="second-eye", charset="UTF-8"'},
            )
            await response(scope, receive, self._secure_send(send))
            return
        if path != "/healthz" and scope.get("method", "GET").upper() not in SAFE_METHODS:
            if not self._same_origin(headers):
                response = JSONResponse({"detail": "跨站写请求已拒绝"}, status_code=403)
                await response(scope, receive, self._secure_send(send))
                return
        await self.app(scope, receive, self._secure_send(send))

    def _authorized(self, raw_header: bytes) -> bool:
        try:
            scheme, encoded = raw_header.decode("latin-1").split(" ", 1)
            if scheme.lower() != "basic":
                return False
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        return secrets.compare_digest(username, self.username) and secrets.compare_digest(
            password, self.password
        )

    @staticmethod
    def _same_origin(headers: dict[bytes, bytes]) -> bool:
        host = headers.get(b"host", b"").decode("latin-1").lower()
        source = headers.get(b"origin") or headers.get(b"referer")
        if not host or not source:
            return False
        try:
            return urlsplit(source.decode("latin-1")).netloc.lower() == host
        except ValueError:
            return False

    @staticmethod
    def _secure_send(send):
        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"cache-control", b"no-store, max-age=0"),
                        (b"pragma", b"no-cache"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
                        ),
                    ]
                )
                message["headers"] = headers
            await send(message)

        return wrapped
