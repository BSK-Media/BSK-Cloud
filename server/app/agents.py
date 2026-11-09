"""Zdalna kontrola programu BSK Backup na komputerach pracowników.

- wersja programu (z nagłówka User-Agent „BSK-Backup/x.y.z”) i automatyczna aktualizacja
  z plików w bsk-dane/instalator (version.txt + pliki .exe),
- „Synchronizuj teraz” z panelu (agent sprawdza ustawienia co ~2 minuty),
- raport z komputera: kolejka, ostatnie błędy i wpisy dziennika,
- e-mail do administratora, gdy komputer nie łączy się z serwerem od kilku dni.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .db import Database, now_iso

log = logging.getLogger("bskcloud.agents")

UPDATE_FILES = ("bsk-backup-agent.exe", "bsk-backup.exe")
_UA = re.compile(r"BSK-Backup/(\d+(?:\.\d+){1,3})")
_sha_cache: dict[str, tuple[float, int, str]] = {}
_sha_lock = threading.Lock()


def version_from_ua(ua: str) -> str:
    m = _UA.search(ua or "")
    return m.group(1) if m else ""


def vtuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except ValueError:
        return ()


def newer(a: str, b: str) -> bool:
    """Czy wersja a jest nowsza od b."""
    ta, tb = vtuple(a), vtuple(b)
    return bool(ta) and bool(tb) and ta > tb


def _sha256(path: Path) -> str:
    st = path.stat()
    key = str(path)
    with _sha_lock:
        hit = _sha_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    with _sha_lock:
        _sha_cache[key] = (st.st_mtime, st.st_size, digest)
    return digest


def installer_version(settings: Settings) -> str:
    try:
        v = (settings.installer_dir / "version.txt").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    return v if vtuple(v) else ""


def update_offer(settings: Settings, agent_version: str) -> dict | None:
    """Nowsza wersja programu w bsk-dane/instalator dla agenta w starszej wersji (z sumami SHA-256)."""
    latest = installer_version(settings)
    if not latest or not newer(latest, agent_version):
        return None
    files = {}
    for name in UPDATE_FILES:
        p = settings.installer_dir / name
        if not p.is_file():
            return None
        files[name] = _sha256(p)
    return {"version": latest, "files": files}


def parse_report(raw: str) -> dict:
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def clean_report(body: dict) -> dict:
    """Raport od agenta — tylko znane pola, z limitami długości (dane z zewnątrz)."""
    def s(v, n=300):
        return str(v)[:n] if v is not None else ""

    def num(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    def when(v):
        """Czas od agenta -> ISO z strefą (bez strefy = UTC); inaczej pusty."""
        try:
            dt = datetime.fromisoformat(str(v)[:40])
        except (TypeError, ValueError):
            return ""
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()

    log_items = []
    for item in (body.get("log") or [])[-40:]:
        if isinstance(item, dict):
            log_items.append({"t": when(item.get("t")), "level": s(item.get("level"), 10), "msg": s(item.get("msg"), 500)})
    errors = []
    for item in (body.get("errors") or [])[:10]:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            errors.append([s(item[0], 400), s(item[1], 300)])
    return {
        "pending": num(body.get("pending")), "done": num(body.get("done")),
        "last_upload": when(body.get("last_upload")) if body.get("last_upload") else "", "folders": [s(f, 300) for f in (body.get("folders") or [])[:20]],
        "paused": s(body.get("paused"), 300), "errors": errors, "log": log_items,
        "update_error": s(body.get("update_error"), 300),
    }


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def check_alerts(settings: Settings, db: Database, send) -> int:
    """E-mail do admina dla komputerów bez kontaktu od `device_alert_days` dni (raz na okres ciszy)."""
    days = settings.device_alert_days
    if days <= 0:
        return 0
    sent = 0
    for d in db.all("SELECT * FROM devices WHERE active = 1 AND last_seen IS NOT NULL"):
        age = _age_days(d["last_seen"])
        if age is None or age < days:
            continue
        if d["alert_sent_at"] and d["alert_sent_at"] > d["last_seen"]:
            continue  # już powiadomiono o tej przerwie
        who = d["employee"] or d["name"]
        send(f"Brak kopii: {who} ({d['name']})",
             f"Komputer {d['name']} ({who}) nie połączył się z serwerem od {int(age)} dni "
             f"(ostatni kontakt: {d['last_seen'][:16].replace('T', ' ')} UTC).\n\n"
             "Możliwe przyczyny: komputer jest wyłączony lub poza siecią, program BSK Backup został zamknięty "
             "albo odinstalowany. Kopie nowych plików z tego komputera nie powstają.",
             f"{settings.public_url}/admin/devices/{d['id']}")
        db.run("UPDATE devices SET alert_sent_at = ? WHERE id = ?", now_iso(), d["id"])
        db.log(None, "device_alert", d["name"])
        sent += 1
    return sent


def start_alerts(settings: Settings, db: Database, send) -> threading.Event:
    stop = threading.Event()

    def loop() -> None:
        stop.wait(600)
        while not stop.is_set():
            try:
                check_alerts(settings, db, send)
            except Exception:  # noqa: BLE001 — alert nie może zatrzymać portalu
                log.exception("Sprawdzanie komputerów nie powiodło się")
            stop.wait(3600)

    threading.Thread(target=loop, name="alerty-komputerow", daemon=True).start()
    return stop
