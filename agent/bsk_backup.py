"""BSK Backup — agent kopii zapasowych dla komputerów pracowników.

Obserwuje wskazane foldery (domyślnie Pulpit i Dokumenty). Każdy nowo utworzony lub zapisany
plik po chwili „uspokojenia” trafia na serwer BSK Media Cloud (NAS). Gdy nie ma internetu albo
NAS jest niedostępny, pliki czekają w trwałej kolejce i zostaną wysłane, gdy połączenie wróci.
Usunięcie pliku na komputerze NIE usuwa go z kopii.

Użycie:
    bsk-backup configure --server https://cloud.bskmedia.pl --token bsk_... [--folder C:\\Projekty ...]
    bsk-backup test        # sprawdza połączenie z serwerem
    bsk-backup run         # działa w tle (tak uruchamia go autostart)
    bsk-backup status      # podsumowanie kolejki i ostatnie wpisy dziennika
    bsk-backup scan        # pełne przeskanowanie folderów teraz
"""
from __future__ import annotations

import argparse
import collections
import fnmatch
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

VERSION = "1.3.0"
CONFIG_REFRESH_S = 120  # co tyle sprawdza ustawienia z panelu (foldery, „Synchronizuj teraz”, aktualizacja)
UPDATE_RETRY_S = 6 * 3600  # nieudana aktualizacja do tej samej wersji — kolejna próba najwcześniej po 6 h
CHUNK_THRESHOLD = 50 * 1024 * 1024  # większe pliki idą w kawałkach (limit Cloudflare: 100 MB na żądanie)
LAN_RECHECK_S = 300
log = logging.getLogger("bsk-backup")


class RecentLog(logging.Handler):
    """Ostatnie ostrzeżenia i błędy w pamięci — trafiają do panelu administratora w raporcie."""

    def __init__(self, size: int = 40):
        super().__init__(logging.WARNING)
        self.items: collections.deque = collections.deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                msg += f" ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
            self.items.append({"t": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                               "level": record.levelname, "msg": msg[:500]})
        except Exception:  # noqa: BLE001
            pass


RECENT = RecentLog()
log.addHandler(RECENT)


def vtuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except ValueError:
        return ()


def utc_iso(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else ""

DEFAULT_EXCLUDES = [
    # pliki tymczasowe i blokady programów
    "~$*", "~*.tmp", "*.tmp", "*.temp", "*.part", "*.crdownload", "*.download", "*.partial",
    ".~lock.*#", "*.swp", "*.lock", "*.lck",
    # pliki systemowe
    "desktop.ini", "Thumbs.db", ".DS_Store", "*.lnk", "*.ini~",
    # katalogi, których nie ma sensu kopiować
    "node_modules", ".git", "__pycache__", ".venv", "venv", "$RECYCLE.BIN", "System Volume Information",
    ".Trash*", "AppData", "Cache", "Caches", ".cache",
]


# ---------------------------------------------------------------- ścieżki i konfiguracja

def app_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "BSKBackup"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BSKBackup"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "bsk-backup"


def default_folders() -> list[Path]:
    home = Path.home()
    candidates = [
        home / "Desktop", home / "Documents", home / "Pulpit", home / "Dokumenty",
        home / "OneDrive" / "Desktop", home / "OneDrive" / "Documents",
        home / "OneDrive" / "Pulpit", home / "OneDrive" / "Dokumenty",
    ]
    seen, out = set(), []
    for c in candidates:
        if c.is_dir():
            r = c.resolve()
            if r not in seen:
                seen.add(r)
                out.append(c)
    return out


# Foldery specjalne ustawiane w panelu (@pulpit…) -> identyfikatory Windows (KNOWNFOLDERID) i zapasowe ścieżki
SPECIAL = {
    "@pulpit": ("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", ["Desktop", "Pulpit"]),
    "@dokumenty": ("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", ["Documents", "Dokumenty"]),
    "@pobrane": ("{374DE290-123F-4565-9164-39C4925E467B}", ["Downloads", "Pobrane"]),
    "@obrazy": ("{33E28130-4E1E-4676-835A-98395C3BC3BB}", ["Pictures", "Obrazy"]),
    "@wideo": ("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", ["Videos", "Wideo", "Filmy"]),
    "@muzyka": ("{4BD8D571-6D19-48D3-BE97-422220080E43}", ["Music", "Muzyka"]),
}


def _known_folder_windows(guid: str) -> Path | None:
    try:
        import ctypes
        from ctypes import wintypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        u = uuid.UUID(guid)
        g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) != 0:
            return None
        path = out.value
        ctypes.windll.ole32.CoTaskMemFree(out)
        return Path(path)
    except Exception:  # noqa: BLE001
        return None


def resolve_folder(entry: str) -> Path | None:
    """„@pulpit” itp. -> prawdziwa ścieżka (z uwzględnieniem OneDrive); zwykła ścieżka bez zmian."""
    entry = entry.strip()
    if not entry.startswith("@"):
        p = Path(os.path.expandvars(entry)).expanduser()
        return p if p.is_dir() else None
    if entry not in SPECIAL:
        return None
    guid, names = SPECIAL[entry]
    if sys.platform == "win32":
        found = _known_folder_windows(guid)
        if found and found.is_dir():
            return found
    home = Path.home()
    for base in (home, home / "OneDrive"):
        for name in names:
            if (base / name).is_dir():
                return base / name
    return None


class Config:
    def __init__(self, data: dict, path: Path):
        self.path = path
        self.server = data.get("server", "").rstrip("/")
        self.lan_server = data.get("lan_server", "").rstrip("/")
        self.token = data.get("token", "")
        self.folders = [{"path": f["path"], "label": f["label"]} for f in data.get("folders", [])]
        self.excludes = data.get("excludes", DEFAULT_EXCLUDES)
        self.settle_seconds = float(data.get("settle_seconds", 10))
        self.scan_interval_minutes = float(data.get("scan_interval_minutes", 60))
        self.max_file_mb = int(data.get("max_file_mb", 20480))
        self.verify_tls = data.get("verify_tls", True)
        self.last_sync = str(data.get("last_sync", ""))  # ostatnia wykonana „Synchronizuj teraz” z panelu
        self.update_attempt = data.get("update_attempt") or {}

    def to_dict(self) -> dict:
        return {
            "server": self.server, "lan_server": self.lan_server, "token": self.token, "folders": self.folders, "excludes": self.excludes,
            "settle_seconds": self.settle_seconds, "scan_interval_minutes": self.scan_interval_minutes,
            "max_file_mb": self.max_file_mb, "verify_tls": self.verify_tls,
            "last_sync": self.last_sync, "update_attempt": self.update_attempt,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        if sys.platform != "win32":
            os.chmod(self.path, 0o600)

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return cls(data, path)


def make_labels(folders: list[Path]) -> list[dict]:
    used: dict[str, int] = {}
    out = []
    for f in folders:
        label = f.name or "Dysk"
        if label.lower() in used:
            used[label.lower()] += 1
            label = f"{label}-{used[label.lower()]}"
        else:
            used[label.lower()] = 1
        out.append({"path": str(f), "label": label})
    return out


# ---------------------------------------------------------------- stan lokalny (SQLite)

class State:
    """Trwała kolejka do wysłania + informacja, co już jest w kopii."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending (path TEXT PRIMARY KEY, due REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS done (path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL,
                sha256 TEXT NOT NULL, uploaded_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_pending_due ON pending(due);
            """
        )

    def enqueue(self, path: str, delay: float = 0) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO pending (path, due) VALUES (?, ?) ON CONFLICT(path) DO UPDATE SET due = MAX(due, excluded.due)",
                (path, time.time() + delay),
            )

    def due(self, limit: int = 100) -> list[tuple[str, int]]:
        with self._lock:
            return self._db.execute(
                "SELECT path, attempts FROM pending WHERE due <= ? ORDER BY due LIMIT ?", (time.time(), limit)
            ).fetchall()

    def reschedule(self, path: str, delay: float, error: str = "", count_attempt: bool = False) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE pending SET due = ?, last_error = ?, attempts = attempts + ? WHERE path = ?",
                (time.time() + delay, error[:500], 1 if count_attempt else 0, path),
            )

    def drop(self, path: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM pending WHERE path = ?", (path,))

    def done_info(self, path: str):
        with self._lock:
            return self._db.execute("SELECT size, mtime, sha256 FROM done WHERE path = ?", (path,)).fetchone()

    def mark_done(self, path: str, size: int, mtime: float, sha: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO done (path, size, mtime, sha256, uploaded_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,"
                " sha256=excluded.sha256, uploaded_at=excluded.uploaded_at",
                (path, size, mtime, sha, time.time()),
            )
            self._db.execute("DELETE FROM pending WHERE path = ?", (path,))

    def counts(self) -> dict:
        with self._lock:
            p = self._db.execute("SELECT COUNT(*), COALESCE(MAX(attempts), 0) FROM pending").fetchone()
            d = self._db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0), MAX(uploaded_at) FROM done").fetchone()
            errs = self._db.execute(
                "SELECT path, last_error FROM pending WHERE last_error != '' ORDER BY attempts DESC LIMIT 5"
            ).fetchall()
        return {"pending": p[0], "max_attempts": p[1], "done": d[0], "done_bytes": d[1], "last_upload": d[2], "errors": errs}


# ---------------------------------------------------------------- logika agenta

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class Agent:
    def __init__(self, config: Config, state: State, session: requests.Session | None = None):
        self.cfg = config
        self.state = state
        self.http = session or requests.Session()
        self.http.headers["Authorization"] = f"Bearer {config.token}"
        self.http.headers["User-Agent"] = f"BSK-Backup/{VERSION}"
        self.http.verify = config.verify_tls
        self.roots = [(Path(f["path"]), f["label"]) for f in config.folders]
        self._config_checked = 0.0
        self.folders_changed = threading.Event()
        self.paused_until = 0.0
        self.offline_backoff = 15.0
        self._base = config.server
        self._base_checked = 0.0
        self.stop = threading.Event()
        self.pause_reason = ""
        self._sync_token = ""  # zlecona synchronizacja, czeka na zakończenie skanu
        self._sync_done = ""
        self.update_error = ""
        self.restart_for_update = False

    # --- filtrowanie
    def excluded(self, path: Path, root: Path) -> bool:
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            return True
        for part in parts:
            for pattern in self.cfg.excludes:
                if fnmatch.fnmatch(part, pattern) or fnmatch.fnmatch(part.lower(), pattern.lower()):
                    return True
        return False

    def root_for(self, path: Path):
        for root, label in self.roots:
            try:
                path.relative_to(root)
                return root, label
            except ValueError:
                continue
        return None, None

    def remote_path(self, path: Path) -> str | None:
        root, label = self.root_for(path)
        if root is None or self.excluded(path, root):
            return None
        return label + "/" + path.relative_to(root).as_posix()

    # --- zdarzenia
    def on_change(self, raw: str) -> None:
        path = Path(raw)
        if self.remote_path(path) is None:
            return
        self.state.enqueue(str(path), delay=self.cfg.settle_seconds)

    def scan(self) -> int:
        added = 0
        for root, _ in self.roots:
            if not root.is_dir():
                log.warning("Folder nie istnieje: %s", root)
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                d = Path(dirpath)
                dirnames[:] = [n for n in dirnames if not self.excluded(d / n, root)]
                for name in filenames:
                    p = d / name
                    if self.excluded(p, root):
                        continue
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    info = self.state.done_info(str(p))
                    if info and info[0] == st.st_size and abs(info[1] - st.st_mtime) < 0.001:
                        continue
                    self.state.enqueue(str(p))
                    added += 1
                if self.stop.is_set():
                    return added
        return added

    # --- wybór serwera: najpierw adres w sieci biurowej (szybciej, bez internetu), potem publiczny
    def base(self) -> str:
        if not self.cfg.lan_server:
            return self.cfg.server
        if time.time() - self._base_checked > LAN_RECHECK_S:
            self._base_checked = time.time()
            try:
                r = self.http.get(self.cfg.lan_server + "/api/agent/ping", timeout=3)
                lan_ok = r.status_code == 200
            except requests.RequestException:
                lan_ok = False
            new = self.cfg.lan_server if lan_ok else self.cfg.server
            if new != self._base:
                log.info("Łączę się przez %s", "sieć biurową" if lan_ok else "internet")
            self._base = new
        return self._base

    def _forget_base(self) -> None:
        self._base_checked = 0.0

    def send_chunked(self, base: str, path: Path, remote: str, sha: str, st):
        """Duży plik w kawałkach; po przerwie wznawia od miejsca, w którym skończył."""
        init = self.http.post(base + "/api/agent/uploads", timeout=30,
                              json={"path": remote, "size": st.st_size, "sha256": sha, "mtime": st.st_mtime})
        if init.status_code != 200 or init.json().get("status") != "upload":
            return init
        info = init.json()
        upload_id, offset, chunk = info["id"], int(info["received"]), int(info["chunk_size"])
        resp = init
        with open(path, "rb") as f:
            while True:
                f.seek(offset)
                data = f.read(chunk)
                resp = self.http.put(f"{base}/api/agent/uploads/{upload_id}", params={"offset": offset}, data=data,
                                     timeout=(15, 900), headers={"Content-Type": "application/octet-stream"})
                if resp.status_code == 409:
                    received = resp.json().get("received")
                    if received is None or received == 0 and offset + len(data) >= st.st_size:
                        return resp  # suma kontrolna się nie zgadza — plik się zmienił
                    offset = int(received)
                    continue
                if resp.status_code != 200:
                    return resp
                body = resp.json()
                if body.get("status") != "partial":
                    return resp
                offset = int(body["received"])

    # --- wysyłka
    def process(self, raw: str, attempts: int) -> None:
        path = Path(raw)
        remote = self.remote_path(path)
        if remote is None or not path.is_file():
            self.state.drop(raw)
            return
        try:
            st = path.stat()
        except OSError:
            self.state.drop(raw)
            return
        age = time.time() - st.st_mtime
        if age < self.cfg.settle_seconds:
            # plik wciąż jest zapisywany — poczekaj aż się „uspokoi”
            self.state.reschedule(raw, self.cfg.settle_seconds - age + 1)
            return
        info = self.state.done_info(raw)
        if info and info[0] == st.st_size and abs(info[1] - st.st_mtime) < 0.001:
            self.state.drop(raw)
            return
        if st.st_size > self.cfg.max_file_mb * 1024 * 1024:
            log.warning("Pomijam (za duży, %d MB): %s", st.st_size // (1024 * 1024), raw)
            self.state.drop(raw)
            return
        try:
            sha = sha256_file(path)
        except (PermissionError, OSError) as exc:
            # plik zablokowany przez inny program (np. otwarty w Outlooku) — spróbuj później
            self.state.reschedule(raw, min(3600, 60 * (attempts + 1)), f"brak dostępu: {exc}", True)
            return
        st2 = path.stat()
        if (st2.st_size, st2.st_mtime) != (st.st_size, st.st_mtime):
            self.state.reschedule(raw, self.cfg.settle_seconds)
            return
        if info and info[2] == sha:
            self.state.mark_done(raw, st.st_size, st.st_mtime, sha)
            return

        base = self.base()
        params = {"path": remote, "sha256": sha, "mtime": f"{st.st_mtime:.3f}"}
        try:
            check = self.http.get(base + "/api/agent/status", params={"path": remote, "sha256": sha}, timeout=30)
            if check.status_code == 200 and not check.json().get("needed", True):
                self.state.mark_done(raw, st.st_size, st.st_mtime, sha)
                return
            self._raise_for_auth(check)
            if st.st_size > CHUNK_THRESHOLD:
                resp = self.send_chunked(base, path, remote, sha, st)
            else:
                with open(path, "rb") as f:
                    resp = self.http.put(base + "/api/agent/files", params=params, data=f, timeout=(15, 900),
                                         headers={"Content-Type": "application/octet-stream"})
        except requests.ConnectionError as exc:
            if base != self.cfg.server:
                self._forget_base()  # sieć biurowa zniknęła (np. laptop wyjechał) — spróbuj przez internet
                self.state.reschedule(raw, 0, "zmiana sieci")
                return
            self.go_offline(str(exc))
            self.state.reschedule(raw, 0, "brak połączenia")
            return
        except requests.Timeout:
            self.state.reschedule(raw, min(3600, 60 * 2 ** min(attempts, 6)), "przekroczony czas", True)
            return
        except AuthError as exc:
            self.pause(900, str(exc))
            self.state.reschedule(raw, 0, str(exc))
            return
        except PermissionError as exc:
            self.state.reschedule(raw, 300, f"brak dostępu: {exc}", True)
            return

        if resp.status_code == 200:
            self.offline_backoff = 15.0
            result = resp.json()
            self.state.mark_done(raw, st.st_size, st.st_mtime, sha)
            log.info("%s: %s (%d B)", "Bez zmian" if result.get("status") == "unchanged" else "Wysłano", remote, st.st_size)
        elif resp.status_code == 409:
            self.state.reschedule(raw, self.cfg.settle_seconds, "plik zmienił się w trakcie wysyłki")
        elif resp.status_code == 507:
            self.pause(3600, "przekroczony limit miejsca na kopie tego komputera — skontaktuj się z administratorem")
            self.state.reschedule(raw, 0, "brak miejsca (limit)")
        elif resp.status_code == 413:
            log.warning("Serwer odrzucił (za duży): %s", remote)
            self.state.drop(raw)
        elif resp.status_code == 401:
            self.pause(900, "token odrzucony przez serwer")
            self.state.reschedule(raw, 0, "token odrzucony")
        elif resp.status_code == 400:
            log.warning("Serwer odrzucił ścieżkę %s: %s", remote, resp.text[:200])
            self.state.drop(raw)
        else:
            self.state.reschedule(raw, min(3600, 30 * 2 ** min(attempts, 7)), f"HTTP {resp.status_code}", True)
            log.warning("Błąd serwera HTTP %s dla %s", resp.status_code, remote)

    @staticmethod
    def _raise_for_auth(resp) -> None:
        if resp.status_code == 401:
            raise AuthError("token odrzucony przez serwer (urządzenie wyłączone lub nowy token)")

    def pause(self, seconds: float, reason: str) -> None:
        log.error("Wstrzymuję wysyłanie na %d min: %s", seconds // 60, reason)
        self.paused_until = time.time() + seconds
        self.pause_reason = reason

    def go_offline(self, reason: str) -> None:
        if self.offline_backoff <= 15:
            log.warning("Serwer niedostępny — pliki czekają w kolejce (%s)", reason[:120])
        self.paused_until = time.time() + self.offline_backoff
        self.offline_backoff = min(600.0, self.offline_backoff * 2)

    def work_once(self) -> int:
        if time.time() < self.paused_until:
            return 0
        n = 0
        for raw, attempts in self.state.due():
            if self.stop.is_set() or time.time() < self.paused_until:
                break
            try:
                self.process(raw, attempts)
            except Exception as exc:  # noqa: BLE001 — agent ma działać mimo pojedynczych błędów
                log.exception("Nieoczekiwany błąd dla %s", raw)
                self.state.reschedule(raw, 600, str(exc), True)
            n += 1
        return n

    # --- pętla główna
    # --- foldery ustawiane przez administratora w panelu
    def refresh_remote_config(self) -> bool:
        """Pobiera listę folderów z portalu. Zwraca True, gdy się zmieniła (trzeba przestawić obserwację)."""
        self._config_checked = time.time()
        try:
            r = self.http.get(self.base() + "/api/agent/config", timeout=15)
            if r.status_code != 200:
                return False
            data = r.json()
        except (requests.RequestException, ValueError):
            return False
        lan = str(data.get("lan_server") or "").rstrip("/")
        if lan and lan != self.cfg.lan_server:  # np. NAS zmienił adres w sieci biurowej — panel jest ważniejszy
            self.cfg.lan_server = lan
            self.cfg.save()
        sync = str(data.get("sync") or "")
        if sync and sync != self.cfg.last_sync and sync != self._sync_token:
            log.info("Synchronizacja zlecona z panelu — skanuję foldery i wysyłam zaległe pliki")
            self._sync_token = sync
            self.paused_until = 0.0  # spróbuj od razu, także po wcześniejszej przerwie
            self.offline_backoff = 15.0
            self.folders_changed.set()
        if isinstance(data.get("update"), dict):
            self.maybe_update(data["update"])
        changed = self._apply_remote_folders(data)
        self.report()
        return changed

    def _apply_remote_folders(self, data: dict) -> bool:
        entries = [e for e in data.get("folders") or [] if isinstance(e, str) and e.strip()]
        if not entries:
            return False  # brak ustawień w panelu — zostają ustawienia lokalne
        resolved, missing = [], []
        for e in entries:
            p = resolve_folder(e)
            (resolved if p else missing).append(p or e)
        if missing:
            log.warning("Nie znaleziono folderów z panelu: %s", ", ".join(map(str, missing)))
        new = make_labels(list(dict.fromkeys(resolved)))
        if not new or [f["path"] for f in new] == [f["path"] for f in self.cfg.folders]:
            return False
        self.cfg.folders = new
        self.cfg.save()
        self.roots = [(Path(f["path"]), f["label"]) for f in new]
        log.info("Nowe foldery z panelu: %s", ", ".join(f["path"] for f in new))
        return True

    # --- raport dla panelu administratora
    def report(self) -> None:
        c = self.state.counts()
        body = {
            "version": VERSION, "pending": c["pending"], "done": c["done"], "last_upload": utc_iso(c["last_upload"]),
            "errors": [[p, e] for p, e in c["errors"]], "log": list(RECENT.items),
            "folders": [str(r) for r, _ in self.roots],
            "paused": self.pause_reason if time.time() < self.paused_until else "",
            "sync_done": self._sync_done, "update_error": self.update_error,
        }
        try:
            self.http.post(self.base() + "/api/agent/report", json=body, timeout=15)
        except requests.RequestException:
            pass

    def _sync_finished(self) -> None:
        if not self._sync_token:
            return
        self.cfg.last_sync = self._sync_done = self._sync_token
        self._sync_token = ""
        self.cfg.save()
        log.info("Synchronizacja z panelu zakończona")
        self.report()

    # --- automatyczna aktualizacja (tylko zbudowany program .exe w Windows)
    @staticmethod
    def can_self_update() -> bool:
        return sys.platform == "win32" and bool(getattr(sys, "frozen", False))

    def maybe_update(self, offer: dict) -> bool:
        version = str(offer.get("version") or "")
        files = offer.get("files") or {}
        if not (vtuple(version) > vtuple(VERSION) and isinstance(files, dict) and files) or not self.can_self_update():
            return False
        att = self.cfg.update_attempt
        if att.get("version") == version and time.time() - float(att.get("at", 0)) < UPDATE_RETRY_S:
            return False
        self.cfg.update_attempt = {"version": version, "at": time.time()}
        self.cfg.save()
        try:
            staged = self.download_update(files)
            self.launch_updater(staged)
        except Exception as exc:  # noqa: BLE001
            self.update_error = f"{version}: {exc}"[:300]
            log.error("Aktualizacja do %s nie powiodła się: %s", version, exc)
            return False
        log.info("Aktualizacja do wersji %s — program uruchomi się ponownie", version)
        self.restart_for_update = True
        self.stop.set()
        return True

    def download_update(self, files: dict) -> dict[str, Path]:
        target_dir = self.cfg.path.parent / "aktualizacja"
        target_dir.mkdir(parents=True, exist_ok=True)
        staged = {}
        for name, sha in files.items():
            if name not in ("bsk-backup-agent.exe", "bsk-backup.exe"):
                continue
            dest = target_dir / (name + ".new")
            h = hashlib.sha256()
            with self.http.get(f"{self.base()}/api/agent/update/{name}", stream=True, timeout=(15, 300)) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"pobieranie {name}: HTTP {r.status_code}")
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        f.write(chunk)
                        h.update(chunk)
            if h.hexdigest() != str(sha).lower():
                dest.unlink(missing_ok=True)
                raise RuntimeError(f"suma kontrolna {name} się nie zgadza")
            staged[name] = dest
        if "bsk-backup-agent.exe" not in staged:
            raise RuntimeError("brak pliku bsk-backup-agent.exe")
        return staged

    def launch_updater(self, staged: dict[str, Path]) -> None:
        """Skrypt PowerShell czeka na zamknięcie programu, podmienia pliki .exe i uruchamia nową wersję."""
        install_dir = Path(sys.executable).resolve().parent
        agent_exe = install_dir / "bsk-backup-agent.exe"

        def q(p) -> str:
            return "'" + str(p).replace("'", "''") + "'"

        pairs = ", ".join(f"@({q(src)}, {q(install_dir / name)})" for name, src in staged.items())
        script = staged["bsk-backup-agent.exe"].parent / "aktualizuj.ps1"
        script.write_text(
            "$ErrorActionPreference = 'SilentlyContinue'\r\n"
            f"Wait-Process -Id {os.getpid()} -Timeout 120\r\n"
            "Start-Sleep -Seconds 1\r\n"
            f"foreach ($pair in @({pairs})) {{\r\n"
            "  for ($i = 0; $i -lt 15; $i++) {\r\n"
            "    try { Copy-Item -LiteralPath $pair[0] -Destination $pair[1] -Force -ErrorAction Stop; break }\r\n"
            "    catch { Start-Sleep -Seconds 2 }\r\n"
            "  }\r\n"
            "}\r\n"
            f"Start-Process -FilePath {q(agent_exe)} -ArgumentList 'run' -WindowStyle Hidden\r\n",
            encoding="utf-8-sig",  # BOM: Windows PowerShell 5.1 poprawnie czyta polskie znaki w ścieżkach
        )
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                          "-File", str(script)], creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _start_observer(self):
        observer = Observer()
        handler = _Handler(self)
        for root, _ in self.roots:
            if root.is_dir():
                observer.schedule(handler, str(root), recursive=True)
                log.info("Obserwuję: %s", root)
        observer.start()
        return observer

    def run(self) -> None:
        self.refresh_remote_config()
        observer = self._start_observer()
        threading.Thread(target=self._scan_loop, daemon=True).start()
        try:
            while not self.stop.is_set():
                if time.time() - self._config_checked > CONFIG_REFRESH_S and self.refresh_remote_config():
                    observer.stop()
                    observer.join(timeout=10)
                    observer = self._start_observer()
                    self.folders_changed.set()  # skan nowych folderów od razu
                if self.work_once() == 0:
                    self.stop.wait(2)
        finally:
            observer.stop()
            observer.join(timeout=10)

    def _scan_loop(self) -> None:
        while not self.stop.is_set():
            self.folders_changed.clear()
            try:
                added = self.scan()
                if added:
                    log.info("Skan: %d plików do sprawdzenia/wysłania", added)
                if not self.stop.is_set():
                    self._sync_finished()
            except Exception:  # noqa: BLE001
                log.exception("Błąd skanowania")
            deadline = time.time() + self.cfg.scan_interval_minutes * 60
            while not self.stop.is_set() and not self.folders_changed.is_set() and time.time() < deadline:
                self.stop.wait(2)


class AuthError(Exception):
    pass


class _Handler(FileSystemEventHandler):
    def __init__(self, agent: Agent):
        self.agent = agent

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event.src_path, event.is_directory)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path, False)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._handle(event.dest_path, event.is_directory)

    def on_closed(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path, False)

    def _handle(self, raw, is_dir: bool) -> None:
        raw = os.fsdecode(raw)
        if is_dir:
            # katalog wklejony/przeniesiony w całości — dodaj jego pliki
            for dirpath, _, files in os.walk(raw):
                for name in files:
                    self.agent.on_change(os.path.join(dirpath, name))
        else:
            self.agent.on_change(raw)


# ---------------------------------------------------------------- uruchamianie

def setup_logging(directory: Path, console: bool) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(directory / "bsk-backup.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    if console and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("watchdog").setLevel(logging.WARNING)


class SingleInstance:
    def __init__(self, path: Path):
        self.fh = open(path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("BSK Backup już działa na tym komputerze.")


def out(msg: str) -> None:
    if sys.stdout is not None:
        print(msg)


def cmd_configure(args, cfg: Config) -> int:
    if args.server:
        cfg.server = args.server.rstrip("/")
    if args.token:
        cfg.token = args.token
    if args.lan_server is not None:
        cfg.lan_server = args.lan_server.rstrip("/")
    if args.folder:
        folders = [Path(f).expanduser() for f in args.folder]
    elif not cfg.folders:
        folders = default_folders()
    else:
        folders = None
    if folders is not None:
        missing = [str(f) for f in folders if not f.is_dir()]
        if missing:
            out("Uwaga, te foldery nie istnieją: " + ", ".join(missing))
        cfg.folders = make_labels(folders)
    if args.insecure:
        cfg.verify_tls = False
    cfg.save()
    out(f"Zapisano konfigurację: {cfg.path}")
    for f in cfg.folders:
        out(f"  {f['path']}  ->  {f['label']}/")
    return 0


def cmd_test(cfg: Config) -> int:
    if not (cfg.server and cfg.token):
        out("Brak konfiguracji — uruchom najpierw: bsk-backup configure --server ... --token ...")
        return 2
    try:
        r = requests.get(cfg.server + "/api/agent/ping", headers={"Authorization": f"Bearer {cfg.token}"},
                         timeout=15, verify=cfg.verify_tls)
    except requests.RequestException as exc:
        out(f"Nie można połączyć się z {cfg.server}: {exc}")
        return 1
    if r.status_code == 200:
        out(f"OK — połączono jako urządzenie „{r.json().get('device')}”.")
        return 0
    out(f"Serwer odpowiedział HTTP {r.status_code}: {r.text[:200]}")
    return 1


def cmd_status(cfg: Config, state: State, logdir: Path) -> int:
    c = state.counts()
    out(f"BSK Backup {VERSION}")
    out(f"Serwer: {cfg.server or '(brak)'}" + (f" (w biurze: {cfg.lan_server})" if cfg.lan_server else ""))
    for f in cfg.folders:
        out(f"Folder: {f['path']} -> {f['label']}/")
    last = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["last_upload"])) if c["last_upload"] else "nigdy"
    out(f"W kopii: {c['done']} plików ({c['done_bytes'] / 1024 / 1024:.1f} MB), ostatnia wysyłka: {last}")
    out(f"Czeka w kolejce: {c['pending']}")
    for path, err in c["errors"]:
        out(f"  ! {path}: {err}")
    logfile = logdir / "bsk-backup.log"
    if logfile.exists():
        out("\nOstatnie wpisy dziennika:")
        for line in logfile.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]:
            out("  " + line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bsk-backup", description="Agent kopii zapasowych BSK Media")
    parser.add_argument("--dir", help="katalog konfiguracji/stanu (domyślnie w profilu użytkownika)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("configure", help="ustaw serwer, token i foldery")
    c.add_argument("--server")
    c.add_argument("--lan-server", help="adres serwera w sieci biurowej, np. http://192.168.10.24:8090")
    c.add_argument("--token")
    c.add_argument("--folder", action="append", help="folder do kopiowania (można podać wiele razy)")
    c.add_argument("--insecure", action="store_true", help="nie sprawdzaj certyfikatu (tylko w LAN)")
    sub.add_parser("test")
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("scan")
    sub.add_parser("version")
    args = parser.parse_args(argv)

    base = Path(args.dir) if args.dir else app_dir()
    cfg = Config.load(base / "config.json")

    if args.cmd == "version":
        out(VERSION)
        return 0
    if args.cmd == "configure":
        return cmd_configure(args, cfg)
    if args.cmd == "test":
        return cmd_test(cfg)

    state = State(base / "state.db")
    if args.cmd == "status":
        return cmd_status(cfg, state, base)

    setup_logging(base, console=sys.stdout is not None and sys.stdout.isatty())
    if not (cfg.server and cfg.token and cfg.folders):
        log.error("Brak konfiguracji (%s). Uruchom: bsk-backup configure ...", cfg.path)
        return 2
    agent = Agent(cfg, state)
    if args.cmd == "scan":
        out(f"Dodano do kolejki: {agent.scan()} plików")
        return 0
    _lock = SingleInstance(base / "agent.lock")  # noqa: F841 — trzyma blokadę do końca procesu
    log.info("BSK Backup %s start, serwer %s", VERSION, cfg.server)
    try:
        agent.run()
    except KeyboardInterrupt:
        agent.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
