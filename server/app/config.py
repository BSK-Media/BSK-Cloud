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
    db_dir: Path | None = None
    backup_dir: Path | None = None
    site_name: str = "BSK Media Cloud"
    cookie_secure: bool = True
    session_hours: int = 12
    max_upload_mb: int = 51200
    max_backup_file_mb: int = 20480
    device_alert_days: int = 3  # e-mail, gdy komputer nie łączy się z serwerem od tylu dni (0 = wyłączone)
    backup_keep_versions: int = 10
    default_quota_mb: int = 0
    admin_username: str = ""
    admin_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    notify_email: str = ""
    public_url: str = ""
    lan_url: str = ""
    office_url: str = ""
    office_internal_url: str = "http://office"
    office_secret: str = ""
    app_internal_url: str = "http://app:8000"
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        """Baza może leżeć na innym (szybszym) dysku niż pliki — BSK_DB_DIR."""
        return (self.db_dir or self.data_dir / "app") / "bskcloud.db"

    @property
    def clients_dir(self) -> Path:
        return self.data_dir / "klienci"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "kopie-pracownikow"

    @property
    def shared_dir(self) -> Path:
        """Folder na NAS z plikami do udostępniania linkiem (wrzucasz tu pliki np. przez SMB)."""
        return self.data_dir / "udostepnione"

    @property
    def installer_dir(self) -> Path:
        """Folder na NAS z plikami programu BSK Backup (zawartość dist/ z builda)."""
        return self.data_dir / "instalator"

    @property
    def transfers_dir(self) -> Path:
        return self.data_dir / "transfery"

    @property
    def backgrounds_dir(self) -> Path:
        """Tła strony pobierania transferów (np. realizacje BSK Media)."""
        return self.data_dir / "transfery" / "_tla"

    @property
    def import_dir(self) -> Path:
        """Folder na NAS (bsk-dane/import), do którego kopiujesz duże archiwa przez sieć lokalną."""
        return self.data_dir / "import"

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "kosz"

    @property
    def thumbs_dir(self) -> Path:
        """Miniaturki na szybkim dysku (obok bazy)."""
        return self.db_path.parent / "miniatury"

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
            db_dir=Path(os.environ["BSK_DB_DIR"]) if os.environ.get("BSK_DB_DIR") else None,
            backup_dir=Path(os.environ["BSK_BACKUP_DIR"]) if os.environ.get("BSK_BACKUP_DIR") else None,
            secret_key=secret,
            site_name=os.environ.get("BSK_SITE_NAME", "BSK Media Cloud"),
            cookie_secure=_bool("BSK_COOKIE_SECURE", True),
            session_hours=_int("BSK_SESSION_HOURS", 12),
            max_upload_mb=_int("BSK_MAX_UPLOAD_MB", 51200),
            max_backup_file_mb=_int("BSK_MAX_BACKUP_FILE_MB", 20480),
            device_alert_days=_int("BSK_DEVICE_ALERT_DAYS", 3),
            backup_keep_versions=_int("BSK_BACKUP_KEEP_VERSIONS", 10),
            default_quota_mb=_int("BSK_DEFAULT_QUOTA_MB", 0),
            admin_username=os.environ.get("BSK_ADMIN_USERNAME", ""),
            admin_password=os.environ.get("BSK_ADMIN_PASSWORD", ""),
            smtp_host=os.environ.get("BSK_SMTP_HOST", ""),
            smtp_port=_int("BSK_SMTP_PORT", 587),
            smtp_user=os.environ.get("BSK_SMTP_USER", ""),
            smtp_password=os.environ.get("BSK_SMTP_PASSWORD", ""),
            smtp_from=os.environ.get("BSK_SMTP_FROM", ""),
            notify_email=os.environ.get("BSK_NOTIFY_EMAIL", ""),
            public_url=os.environ.get("BSK_PUBLIC_URL", "https://cloud.bskmedia.pl"),
            lan_url=os.environ.get("BSK_LAN_URL", ""),
            office_url=os.environ.get("BSK_OFFICE_URL", "").rstrip("/"),
            office_internal_url=os.environ.get("BSK_OFFICE_INTERNAL_URL", "http://office").rstrip("/"),
            office_secret=os.environ.get("BSK_OFFICE_SECRET", ""),
            app_internal_url=os.environ.get("BSK_APP_INTERNAL_URL", "http://app:8000").rstrip("/"),
        )
