"""Konfiguracja z zmiennych środowiskowych (plik .env w docker-compose)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "tak", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass
class Settings:
    data_dir: Path
    secret_key: str
    site_name: str = "BSK Media Cloud"
    cookie_secure: bool = True
    session_hours: int = 12
    max_upload_mb: int = 5120
    max_backup_file_mb: int = 20480
    backup_keep_versions: int = 10
    admin_username: str = ""
    admin_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    notify_email: str = ""
    public_url: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app" / "bskcloud.db"

    @property
    def clients_dir(self) -> Path:
        return self.data_dir / "klienci"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "kopie-pracownikow"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "app" / "tmp"

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("BSK_SECRET_KEY", "")
        if len(secret) < 32:
            raise RuntimeError(
                "Ustaw BSK_SECRET_KEY (min. 32 losowe znaki), np.: openssl rand -hex 32"
            )
        return cls(
            data_dir=Path(os.environ.get("BSK_DATA_DIR", "/data")),
            secret_key=secret,
            site_name=os.environ.get("BSK_SITE_NAME", "BSK Media Cloud"),
            cookie_secure=_bool("BSK_COOKIE_SECURE", True),
            session_hours=_int("BSK_SESSION_HOURS", 12),
            max_upload_mb=_int("BSK_MAX_UPLOAD_MB", 5120),
            max_backup_file_mb=_int("BSK_MAX_BACKUP_FILE_MB", 20480),
            backup_keep_versions=_int("BSK_BACKUP_KEEP_VERSIONS", 10),
            admin_username=os.environ.get("BSK_ADMIN_USERNAME", ""),
            admin_password=os.environ.get("BSK_ADMIN_PASSWORD", ""),
            smtp_host=os.environ.get("BSK_SMTP_HOST", ""),
            smtp_port=_int("BSK_SMTP_PORT", 587),
            smtp_user=os.environ.get("BSK_SMTP_USER", ""),
            smtp_password=os.environ.get("BSK_SMTP_PASSWORD", ""),
            smtp_from=os.environ.get("BSK_SMTP_FROM", ""),
            notify_email=os.environ.get("BSK_NOTIFY_EMAIL", ""),
            public_url=os.environ.get("BSK_PUBLIC_URL", "https://cloud.bskmedia.pl"),
        )
