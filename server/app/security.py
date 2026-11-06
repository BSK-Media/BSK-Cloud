"""Hasła, tokeny urządzeń, CSRF i ograniczanie prób logowania."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

import bcrypt

MIN_PASSWORD_LEN = 10


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode())
    except ValueError:
        return False


# Stały hash do porównania, gdy użytkownik nie istnieje (brak wycieku przez czas odpowiedzi).
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def verify_dummy() -> None:
    verify_password("x", _DUMMY_HASH)


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LEN:
        return f"Hasło musi mieć co najmniej {MIN_PASSWORD_LEN} znaków."
    if password.lower() == password or password.upper() == password or not any(
        ch.isdigit() for ch in password
    ):
        return "Hasło musi zawierać małe i wielkie litery oraz cyfrę."
    return None


def generate_password() -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(14))
        if password_problem(pw) is None:
            return pw


def new_device_token() -> str:
    return "bsk_" + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_csrf() -> str:
    return secrets.token_urlsafe(32)


def csrf_ok(expected: str | None, given: str | None) -> bool:
    return bool(expected) and bool(given) and hmac.compare_digest(expected, given)


class LoginLimiter:
    """Blokada po zbyt wielu nieudanych próbach (na login i na adres IP)."""

    def __init__(self, per_user: int = 8, per_ip: int = 25, window_s: int = 900):
        self.per_user = per_user
        self.per_ip = per_ip
        self.window = window_s
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _count(self, key: str) -> int:
        q = self._fails[key]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return len(q)

    def blocked(self, username: str, ip: str) -> bool:
        with self._lock:
            return (
                self._count("u:" + username.lower()) >= self.per_user
                or self._count("ip:" + ip) >= self.per_ip
            )

    def fail(self, username: str, ip: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._fails["u:" + username.lower()].append(now)
            self._fails["ip:" + ip].append(now)

    def success(self, username: str) -> None:
        with self._lock:
            self._fails.pop("u:" + username.lower(), None)
