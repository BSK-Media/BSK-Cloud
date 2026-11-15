"""Szyfrowanie kluczy API i haseł integracji w bazie (Fernet, klucz wyprowadzony z BSK_SECRET_KEY).

Kopia bazy na drugim dysku zawiera tylko zaszyfrowane wartości; bez BSK_SECRET_KEY nie da się ich odczytać.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc1:"


class SecretBox:
    def __init__(self, secret_key: str):
        key = hashlib.sha256(("bsk-agent-secrets:" + secret_key).encode()).digest()
        self._f = Fernet(base64.urlsafe_b64encode(key))

    def seal(self, value: str) -> str:
        return PREFIX + self._f.encrypt(value.encode()).decode() if value else ""

    def open(self, value: str | None) -> str:
        if not value:
            return ""
        if not value.startswith(PREFIX):
            return value
        try:
            return self._f.decrypt(value[len(PREFIX):].encode()).decode()
        except InvalidToken:
            return ""


def mask(value: str) -> str:
    """Do wyświetlenia w panelu: tylko końcówka klucza."""
    if not value:
        return ""
    return "•" * 8 + value[-4:] if len(value) > 8 else "•" * len(value)
