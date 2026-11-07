"""Wysyłanie dużych plików w kawałkach (omija limit 100 MB na żądanie w Cloudflare, pozwala wznawiać).

Przebieg: utworzenie sesji wysyłki (nazwa, rozmiar) -> kolejne kawałki PUT ?offset=N (po kolei) ->
po ostatnim kawałku plik jest kompletny w katalogu tymczasowym na tym samym dysku co dane, z policzoną
sumą SHA-256. Właściwe miejsce docelowe wybiera endpoint, który „odbiera” gotową wysyłkę.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from pathlib import Path

from .db import Database, now_iso

CHUNK_SIZE = 50 * 1024 * 1024  # rozmiar zalecany klientom (Cloudflare przyjmuje do 100 MB na żądanie)
CHUNK_MAX = 64 * 1024 * 1024  # twardy limit pojedynczego kawałka
MAX_AGE_S = 24 * 3600  # nieukończone/nieodebrane wysyłki są usuwane po dobie

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    user_id INTEGER,
    device_id INTEGER,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    received INTEGER NOT NULL DEFAULT 0,
    expected_sha256 TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    mtime REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'done')),
    created_at TEXT NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_device ON uploads(device_id, target);
"""


class ChunkError(Exception):
    def __init__(self, status: int, detail: str, received: int | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.received = received


class ChunkStore:
    def __init__(self, db: Database, tmp_dir: Path):
        self.db = db
        self.dir = tmp_dir / "chunks"
        self.dir.mkdir(parents=True, exist_ok=True)
        db.conn().executescript(SCHEMA)
        cols = {r["name"] for r in db.all("PRAGMA table_info(uploads)")}
        if "dest" not in cols:  # dokąd zapisać plik po ostatnim kawałku (wysyłka z przeglądarki)
            db.run("ALTER TABLE uploads ADD COLUMN dest TEXT NOT NULL DEFAULT ''")
        self._hashers: dict[str, "hashlib._Hash"] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def path(self, upload_id: str) -> Path:
        return self.dir / f"{upload_id}.part"

    def create(self, *, name: str, size: int, user_id: int | None = None, device_id: int | None = None,
               expected_sha256: str = "", target: str = "", mtime: float = 0, dest: str = "") -> dict:
        self.cleanup()
        upload_id = secrets.token_urlsafe(18)
        self.db.run(
            "INSERT INTO uploads (id, user_id, device_id, name, size, expected_sha256, target, mtime, created_at, updated_ts, dest)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            upload_id, user_id, device_id, name[:300], size, expected_sha256.lower(), target, mtime, now_iso(), time.time(), dest,
        )
        self.path(upload_id).touch()
        self._hashers[upload_id] = hashlib.sha256()
        if size == 0:
            self.db.run("UPDATE uploads SET status = 'done', sha256 = ? WHERE id = ?",
                        hashlib.sha256().hexdigest(), upload_id)
        return self.get(upload_id)

    def get(self, upload_id: str):
        return self.db.one("SELECT * FROM uploads WHERE id = ?", upload_id)

    def find_resumable(self, device_id: int, target: str, sha256: str):
        return self.db.one(
            "SELECT * FROM uploads WHERE device_id = ? AND target = ? AND expected_sha256 = ? AND status = 'active'"
            " ORDER BY updated_ts DESC LIMIT 1",
            device_id, target, sha256.lower(),
        )

    def _hasher(self, upload) -> "hashlib._Hash":
        h = self._hashers.get(upload["id"])
        if h is None:
            # po restarcie serwera: odtwórz stan sumy kontrolnej z już zapisanej części
            h = hashlib.sha256()
            with open(self.path(upload["id"]), "rb") as f:
                remaining = upload["received"]
                while remaining > 0:
                    data = f.read(min(1024 * 1024, remaining))
                    if not data:
                        break
                    h.update(data)
                    remaining -= len(data)
            self._hashers[upload["id"]] = h
        return h

    async def write(self, upload, offset: int, stream) -> dict:
        """Dopisuje kawałek. Zwraca aktualny stan wysyłki (po ostatnim kawałku status 'done')."""
        uid = upload["id"]
        lock = self._locks.setdefault(uid, asyncio.Lock())
        async with lock:
            upload = self.get(uid)
            if not upload:
                raise ChunkError(404, "Wysyłka wygasła — zacznij od nowa.")
            if upload["status"] == "done":
                return dict(upload)
            if offset != upload["received"]:
                raise ChunkError(409, "Nieprawidłowe miejsce wznowienia.", upload["received"])
            path = self.path(uid)
            if not path.exists():
                raise ChunkError(404, "Wysyłka wygasła — zacznij od nowa.")
            base = self._hasher(upload)
            h = base.copy()
            written = 0
            try:
                with open(path, "r+b") as f:
                    f.seek(offset)
                    async for data in stream:
                        written += len(data)
                        if written > CHUNK_MAX or offset + written > upload["size"]:
                            raise ChunkError(413, "Kawałek jest za duży.", offset)
                        h.update(data)
                        f.write(data)
                    f.truncate(offset + written)
            except BaseException:
                # zerwane połączenie lub błąd — wracamy do stanu sprzed kawałka
                with open(path, "r+b") as f:
                    f.truncate(offset)
                raise
            received = offset + written
            self._hashers[uid] = h
            if received == upload["size"]:
                digest = h.hexdigest()
                if upload["expected_sha256"] and digest != upload["expected_sha256"]:
                    self.reset(uid)
                    raise ChunkError(409, "Suma kontrolna się nie zgadza — plik zmienił się w trakcie wysyłki.", 0)
                self.db.run(
                    "UPDATE uploads SET received = ?, sha256 = ?, status = 'done', updated_ts = ? WHERE id = ?",
                    received, digest, time.time(), uid,
                )
                self._hashers.pop(uid, None)
            else:
                self.db.run("UPDATE uploads SET received = ?, updated_ts = ? WHERE id = ?", received, time.time(), uid)
            return dict(self.get(uid))

    def reset(self, upload_id: str) -> None:
        with open(self.path(upload_id), "wb"):
            pass
        self._hashers[upload_id] = hashlib.sha256()
        self.db.run("UPDATE uploads SET received = 0, updated_ts = ? WHERE id = ?", time.time(), upload_id)

    def remove(self, upload_id: str) -> None:
        self.path(upload_id).unlink(missing_ok=True)
        self.db.run("DELETE FROM uploads WHERE id = ?", upload_id)
        self._hashers.pop(upload_id, None)
        self._locks.pop(upload_id, None)

    _last_cleanup = 0.0

    def cleanup(self) -> None:
        if time.time() - self._last_cleanup < 300:  # przy tysiącach plików nie sprzątamy przy każdym
            return
        self._last_cleanup = time.time()
        cutoff = time.time() - MAX_AGE_S
        for row in self.db.all("SELECT id FROM uploads WHERE updated_ts < ?", cutoff):
            self.remove(row["id"])
        known = {r["id"] for r in self.db.all("SELECT id FROM uploads")}
        for p in self.dir.glob("*.part"):
            if p.stem not in known and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)

    def take_done(self, upload_id: str, user_id: int):
        """Zwraca gotową wysyłkę użytkownika (do przeniesienia na miejsce docelowe) albo None."""
        row = self.get(upload_id)
        if not row or row["user_id"] != user_id or row["status"] != "done" or not self.path(upload_id).is_file():
            return None
        return row

