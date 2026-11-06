"""Powiadomienia e-mail (SMTP, np. Hostinger: smtp.hostinger.com, port 465 SSL lub 587 STARTTLS).

- do administratora: nowe pliki i wiadomości od klientów,
- do klienta: odpowiedź BSK Media, nowe pliki udostępnione klientowi.
Żeby nie zasypać skrzynki, z jednej rozmowy wychodzi najwyżej jeden e-mail na 5 minut.
"""
from __future__ import annotations

import html
import logging
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .config import Settings

log = logging.getLogger("bskcloud.notify")
THROTTLE_S = 300
_last_sent: dict[str, float] = {}
_lock = threading.Lock()


def configured(settings: Settings) -> bool:
    return bool(settings.smtp_host and (settings.smtp_from or settings.smtp_user))


def _html(settings: Settings, title: str, lines: list[str], link: str, button: str) -> str:
    body = "".join(f'<p style="margin:0 0 10px">{html.escape(line)}</p>' for line in lines if line)
    return f"""<!doctype html><html><body style="margin:0;background:#f5f6f8;font-family:Segoe UI,Arial,sans-serif;color:#111827">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:28px 12px"><tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;border:1px solid #e7e9ee;border-radius:14px">
<tr><td style="padding:22px 26px;border-bottom:1px solid #f1f3f6">
<span style="display:inline-block;background:#2a78d6;color:#fff;font-weight:800;font-size:12px;padding:7px 8px;border-radius:8px">BSK</span>
<b style="font-size:15px;margin-left:8px">{html.escape(settings.site_name)}</b></td></tr>
<tr><td style="padding:24px 26px;font-size:15px;line-height:1.55">
<h2 style="margin:0 0 14px;font-size:18px">{html.escape(title)}</h2>{body}
<p style="margin:22px 0 4px"><a href="{html.escape(link)}" style="background:#2a78d6;color:#fff;text-decoration:none;padding:11px 20px;border-radius:10px;font-weight:600;display:inline-block">{html.escape(button)}</a></p>
</td></tr>
<tr><td style="padding:14px 26px;color:#6b7280;font-size:12px;border-top:1px solid #f1f3f6">Wiadomość wysłana automatycznie z {html.escape(settings.public_url)}</td></tr>
</table></td></tr></table></body></html>"""


def _deliver(settings: Settings, to: str, subject: str, text: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    sender = settings.smtp_from or settings.smtp_user
    msg["From"] = formataddr((settings.site_name, sender))
    msg["To"] = to
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, 465, timeout=20, context=context) as s:
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
            s.starttls(context=context)
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)


def send(settings: Settings, to: str, subject: str, lines: list[str], link: str, button: str,
         throttle_key: str | None = None, wait: bool = False) -> str | None:
    """Wysyła e-mail (domyślnie w tle). Zwraca opis błędu przy wait=True, inaczej None."""
    if not (configured(settings) and to):
        return "Poczta nie jest skonfigurowana."
    if throttle_key:
        with _lock:
            if time.time() - _last_sent.get(throttle_key, 0) < THROTTLE_S:
                return None
            _last_sent[throttle_key] = time.time()
    full_subject = f"[{settings.site_name}] {subject}"
    text = "\n\n".join(line for line in lines if line) + f"\n\n{button}: {link}\n"
    html_body = _html(settings, subject, lines, link, button)

    def run() -> str | None:
        try:
            _deliver(settings, to, full_subject, text, html_body)
            return None
        except Exception as exc:  # noqa: BLE001 — powiadomienie nie może psuć głównej operacji
            log.exception("Nie udało się wysłać e-maila do %s", to)
            return f"{type(exc).__name__}: {exc}"

    if wait:
        return run()
    threading.Thread(target=run, daemon=True).start()
    return None


def notify_admin(settings: Settings, subject: str, body: str, link: str = "", throttle_key: str | None = None) -> None:
    send(settings, settings.notify_email, subject, body.split("\n\n"), link or settings.public_url,
         "Otwórz w panelu", throttle_key)
