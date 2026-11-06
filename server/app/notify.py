"""Opcjonalne powiadomienia e-mail dla administratora (gdy skonfigurowano SMTP)."""
from __future__ import annotations

import logging
import smtplib
import threading
from email.message import EmailMessage

from .config import Settings

log = logging.getLogger("bskcloud.notify")


def notify_admin(settings: Settings, subject: str, body: str) -> None:
    if not (settings.smtp_host and settings.notify_email):
        return

    def send() -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[{settings.site_name}] {subject}"
        msg["From"] = settings.smtp_from or settings.smtp_user
        msg["To"] = settings.notify_email
        msg.set_content(body)
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
                s.starttls()
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
        except Exception:  # noqa: BLE001 — powiadomienie nie może psuć głównej operacji
            log.exception("Nie udało się wysłać powiadomienia e-mail")

    threading.Thread(target=send, daemon=True).start()
