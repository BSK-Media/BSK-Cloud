"""Codzienna kopia najważniejszych danych na drugi dysk (np. SSD 1 TB).

- baza danych: spójna migawka SQLite (backup API), 14 ostatnich dni,
- pliki klientów i pliki do linków: kopiowane tylko nowe/zmienione; pliki usunięte w portalu
  zostają w kopii (można je odzyskać po pomyłce).
Kopie pracowników są pomijane — to już są kopie plików z ich komputerów.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import Settings
from .db import Database, now_iso

log = logging.getLogger("bskcloud.backup")
KEEP_DB_COPIES = 14
INTERVAL_S = 24 * 3600
LOW_SPACE_RATIO = 0.10


def _mirror(src: Path, dst: Path, stop: threading.Event) -> tuple[int, int]:
    """Kopiuje nowe i zmienione pliki (rozmiar/czas modyfikacji). Niczego w kopii nie usuwa."""
    copied = copied_bytes = 0
    if not src.is_dir():
        return 0, 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "@", "#"))]
        rel_dir = Path(dirpath).relative_to(src)
        for name in filenames:
            if stop.is_set():
                return copied, copied_bytes
            s = Path(dirpath) / name
            d = dst / rel_dir / name
            try:
                st = s.stat()
                if d.exists():
                    dt = d.stat()
                    if dt.st_size == st.st_size and int(dt.st_mtime) == int(st.st_mtime):
                        continue
                d.parent.mkdir(parents=True, exist_ok=True)
                tmp = d.with_name(d.name + ".kopiowanie")
                shutil.copy2(s, tmp)
                os.replace(tmp, d)
                copied += 1
                copied_bytes += st.st_size
            except OSError as exc:
                log.warning("Nie skopiowano %s: %s", s, exc)
    return copied, copied_bytes


def run_backup(settings: Settings, db: Database, stop: threading.Event | None = None) -> dict:
    stop = stop or threading.Event()
    target = settings.backup_dir
    assert target is not None
    started = time.time()
    db_dir = target / "baza"
    db_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d")
    snapshot = db_dir / f"bskcloud-{stamp}.db"
    tmp = snapshot.with_suffix(".tmp")
    src = sqlite3.connect(settings.db_path)
    dst = sqlite3.connect(tmp)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    os.replace(tmp, snapshot)
    for old in sorted(db_dir.glob("bskcloud-*.db"))[:-KEEP_DB_COPIES]:
        old.unlink(missing_ok=True)

    files = bytes_ = 0
    for name, src_dir in (("klienci", settings.clients_dir), ("udostepnione", settings.shared_dir)):
        n, b = _mirror(src_dir, target / name, stop)
        files += n
        bytes_ += b

    usage = shutil.disk_usage(target)
    result = {
        "finished_at": now_iso(),
        "seconds": round(time.time() - started, 1),
        "copied_files": files,
        "copied_bytes": bytes_,
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "low_space": usage.free < usage.total * LOW_SPACE_RATIO,
        "error": "",
    }
    (target / "ostatnia-kopia.json").write_text(json.dumps(result), encoding="utf-8")
    db.log(None, "backup_done", f"{files} plików, {bytes_} B, wolne {usage.free} B")
    return result


def last_status(settings: Settings) -> dict | None:
    if not settings.backup_dir:
        return None
    try:
        return json.loads((settings.backup_dir / "ostatnia-kopia.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"finished_at": "", "error": "Kopia jeszcze się nie wykonała."}


def start_scheduler(settings: Settings, db: Database) -> threading.Event:
    """Wątek w tle: pierwsza kopia kilka minut po starcie, potem co 24 godziny."""
    stop = threading.Event()

    def loop() -> None:
        stop.wait(300)
        while not stop.is_set():
            status = last_status(settings) or {}
            last = status.get("finished_at") or ""
            due = True
            if last and not status.get("error"):
                try:
                    due = time.time() - datetime.fromisoformat(last).timestamp() > INTERVAL_S - 600
                except ValueError:
                    due = True
            if due:
                try:
                    run_backup(settings, db, stop)
                except Exception as exc:  # noqa: BLE001 — błąd kopii nie może zatrzymać portalu
                    log.exception("Kopia nie powiodła się")
                    try:
                        (settings.backup_dir / "ostatnia-kopia.json").write_text(
                            json.dumps({"finished_at": now_iso(), "error": str(exc)[:300]}), encoding="utf-8")
                    except OSError:
                        pass
                    db.log(None, "backup_failed", str(exc)[:300])
            stop.wait(3600)

    threading.Thread(target=loop, name="kopia-zapasowa", daemon=True).start()
    return stop
