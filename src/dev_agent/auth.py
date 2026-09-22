from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from http.cookies import SimpleCookie
from typing import Optional


class SessionAuth:
    """Sessão curta, assinada e sem dados sensíveis no cookie."""

    cookie_name = "meu_agente_session"
    session_seconds = 60 * 60 * 24 * 7

    def __init__(self, password: str, session_secret: str, cookie_secure: bool = False):
        self.password = password
        self.secret = session_secret.encode("utf-8")
        self.cookie_secure = cookie_secure

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def login(self, password: str) -> Optional[str]:
        if not self.enabled:
            return None
        if not hmac.compare_digest(password, self.password):
            return None
        now = int(time.time())
        payload = f"{now}.{secrets.token_urlsafe(18)}"
        signature = self._sign(payload)
        return f"{payload}.{signature}"

    def authenticated(self, cookie_header: str) -> bool:
        if not self.enabled:
            return True
        cookie = SimpleCookie()
        cookie.load(cookie_header or "")
        morsel = cookie.get(self.cookie_name)
        if not morsel:
            return False
        parts = morsel.value.split(".")
        if len(parts) != 3:
            return False
        timestamp, nonce, signature = parts
        payload = f"{timestamp}.{nonce}"
        if not hmac.compare_digest(signature, self._sign(payload)):
            return False
        try:
            return int(timestamp) + self.session_seconds >= int(time.time())
        except ValueError:
            return False

    def cookie(self, token: str, expires: bool = True) -> str:
        attributes = [f"{self.cookie_name}={token if expires else ''}", "Path=/", "HttpOnly", "SameSite=Strict"]
        if self.cookie_secure:
            attributes.append("Secure")
        if not expires:
            attributes.append("Max-Age=0")
        return "; ".join(attributes)

    def _sign(self, payload: str) -> str:
        digest = hmac.new(self.secret, payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
