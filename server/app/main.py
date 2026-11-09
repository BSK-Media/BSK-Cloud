"""BSK Media Cloud — portal plików dla klientów + odbiór kopii zapasowych z komputerów pracowników."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
import os
import re
import secrets
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from . import agents, backup, charts, chunks, office, security, storage, thumbs, transfers
from .config import Settings
from .db import Database, iso_in, now_iso
from . import notify
from .notify import notify_admin

log = logging.getLogger("bskcloud")
BASE = Path(__file__).parent
REMEMBER_DAYS = 30
USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,39}$")
TRASH_DAYS = 30
SPECIAL_FOLDERS = {
    "@pulpit": "Pulpit",
    "@dokumenty": "Dokumenty",
    "@pobrane": "Pobrane",
    "@obrazy": "Obrazy",
    "@wideo": "Wideo",
    "@muzyka": "Muzyka",
}


def _local_dt(v: str | None) -> str:
    """Czas z bazy (UTC) w strefie Europe/Warsaw, format dd.mm.rrrr gg:mm."""
    if not v:
        return ""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(v).astimezone(ZoneInfo("Europe/Warsaw")).strftime("%d.%m.%Y %H:%M")
    except Exception:  # noqa: BLE001
        return v[:16].replace("T", " ")


def _ago(v: str | None) -> str:
    if not v:
        return "nigdy"
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(v)
    except ValueError:
        return v
    s = int(delta.total_seconds())
    if s < 60:
        return "przed chwilą"
    if s < 3600:
        return f"{s // 60} min temu"
    if s < 86400:
        return f"{s // 3600} godz. temu"
    if s < 86400 * 30:
        return f"{s // 86400} dni temu"
    return _local_dt(v)[:10]


def _initials(name: str | None) -> str:
    parts = [p for p in (name or "?").replace(".", " ").replace("_", " ").split() if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def _file_kind(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    kinds = {
        "img": {"jpg", "jpeg", "png", "gif", "webp", "tif", "tiff", "bmp", "svg", "heic", "psd", "ai", "eps"},
        "video": {"mp4", "mov", "avi", "mkv", "webm", "m4v", "prproj", "aep"},
        "audio": {"mp3", "wav", "aac", "flac", "m4a", "ogg"},
        "sheet": {"xlsx", "xls", "ods", "csv", "numbers"},
        "doc": {"docx", "doc", "odt", "rtf", "txt", "pages", "md"},
        "slides": {"pptx", "ppt", "odp", "key"},
        "pdf": {"pdf"},
        "zip": {"zip", "rar", "7z", "tar", "gz"},
    }
    for kind, exts in kinds.items():
        if ext in exts:
            return kind
    return "file"


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    for d in (settings.clients_dir, settings.backups_dir, settings.shared_dir, settings.installer_dir, settings.tmp_dir):
        d.mkdir(parents=True, exist_ok=True)
    # Duże uploady z formularza trafiają najpierw do katalogu tymczasowego — trzymamy go na dysku NAS.
    os.environ["TMPDIR"] = str(settings.tmp_dir)
    tempfile.tempdir = str(settings.tmp_dir)

    db = Database(settings.db_path)
    chunk_store = chunks.ChunkStore(db, settings.tmp_dir)
    chunk_store.cleanup()
    limiter = security.LoginLimiter()
    _bootstrap_admin(db, settings)
    if settings.backup_dir and os.environ.get("BSK_BACKUP_SCHEDULER", "1") == "1":
        backup.start_scheduler(settings, db)
    if os.environ.get("BSK_ALERT_SCHEDULER", "1") == "1":
        agents.start_alerts(settings, db, lambda subject, body, link: notify_admin(settings, subject, body, link))

    app = FastAPI(title=settings.site_name, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    templates = Jinja2Templates(directory=str(BASE / "templates"))
    templates.env.filters["size"] = storage.human_size
    templates.env.filters["dt"] = _local_dt
    templates.env.filters["ago"] = _ago
    templates.env.filters["gb"] = lambda mb: (f"{(mb or 0) / 1024:.3f}".rstrip("0").rstrip(".") or "0")
    templates.env.filters["initials"] = _initials
    templates.env.filters["ftype"] = _file_kind
    templates.env.globals["charts"] = charts
    templates.env.globals["office_enabled"] = bool(settings.office_url and settings.office_secret)
    templates.env.globals["doc_type"] = office.doc_type
    templates.env.globals["nas_host"] = urllib.parse.urlsplit(settings.lan_url).hostname or "IP-NAS"
    templates.env.globals["site_name"] = settings.site_name
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
    # numer wersji plików statycznych — po aktualizacji przeglądarka i Cloudflare pobiorą nowe style
    asset_hash = hashlib.sha256()
    for f in sorted((BASE / "static").iterdir()):
        if f.is_file():
            asset_hash.update(f.read_bytes())
    templates.env.globals["asset_v"] = asset_hash.hexdigest()[:10]

    max_upload = settings.max_upload_mb * 1024 * 1024
    max_backup = settings.max_backup_file_mb * 1024 * 1024

    # ------------------------------------------------------------------ middleware

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        length = request.headers.get("content-length")
        if length and length.isdigit():
            limit = max_backup if path.startswith("/api/agent/") else max_upload + 1024 * 1024
            if request.method in ("POST", "PUT") and int(length) > limit:
                return JSONResponse({"detail": "Plik jest za duży."}, status_code=413)
        response = await call_next(request)
        if request.headers.get("x-requested-with") == "fetch" and response.status_code == 303:
            # wysyłka z JS: zwróć adres docelowy zamiast przekierowania, żeby komunikat (np. link) nie
            # został „zużyty” przez niewidoczne dla użytkownika żądanie
            response = JSONResponse({"redirect": response.headers["location"]})
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        )
        if path.startswith(("/files", "/admin", "/account", "/s/", "/instaluj")):
            response.headers.setdefault("Cache-Control", "no-store")
        if path.startswith(("/s/", "/instaluj", "/t/")):
            response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="bsk_session",
        max_age=REMEMBER_DAYS * 86400,  # górna granica; właściwa ważność w sesji („exp”)
        same_site="lax",
        https_only=settings.cookie_secure,
    )

    @app.exception_handler(Redirect)
    async def _redirect(_: Request, exc: Redirect):
        return RedirectResponse(exc.url, status_code=303)

    # ------------------------------------------------------------------ helpers

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    def csrf_token(request: Request) -> str:
        tok = request.session.get("csrf")
        if not tok:
            tok = security.new_csrf()
            request.session["csrf"] = tok
        return tok

    def check_csrf(request: Request, given: str | None) -> None:
        if not security.csrf_ok(request.session.get("csrf"), given):
            raise HTTPException(400, "Sesja wygasła — odśwież stronę i spróbuj ponownie.")

    def flash(request: Request, message: str, kind: str = "ok") -> None:
        request.session.setdefault("flash", []).append([kind, message])

    def current_user(request: Request):
        uid = request.session.get("uid")
        if not uid:
            return None
        user = db.one("SELECT * FROM users WHERE id = ?", uid)
        if (not user or not user["active"] or user["session_version"] != request.session.get("sv")
                or request.session.get("exp", 0) < time.time()):
            request.session.clear()
            return None
        return user

    def require_user(request: Request, role: str | None = None, allow_pw_change: bool = False):
        user = current_user(request)
        if not user:
            raise Redirect("/login")
        if user["must_change_password"] and not allow_pw_change:
            raise Redirect("/account")
        if role and user["role"] != role:
            raise HTTPException(403, "Brak dostępu.")
        return user

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        ctx.setdefault("user", current_user(request))
        ctx.setdefault("nav", "")
        if ctx["user"] and not ctx["user"]["must_change_password"]:
            ctx.setdefault("unread_total", unread_for(ctx["user"]))
        ctx["csrf"] = csrf_token(request)
        ctx["flashes"] = request.session.pop("flash", [])
        ctx["max_upload_mb"] = settings.max_upload_mb
        ctx["new_links"] = request.session.pop("new_link", None)
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def space_of(user):
        """Przestrzeń, w której pracuje użytkownik: osoba z firmy klienta -> konto główne firmy."""
        if user["role"] == "client" and user["space_id"]:
            owner = db.one("SELECT * FROM users WHERE id = ?", user["space_id"])
            if owner:
                return owner
        return user

    def sid(user) -> int:
        return space_of(user)["id"]

    def space_members(owner_id: int):
        return db.all("SELECT * FROM users WHERE space_id = ? ORDER BY display_name, username", owner_id)

    def client_or_404(client_id: int):
        c = db.one("SELECT * FROM users WHERE id = ? AND role = 'client' AND space_id IS NULL", client_id)
        if not c:
            raise HTTPException(404, "Nie ma takiego klienta.")
        return c

    def client_dir(client) -> Path:
        """Przestrzeń na NAS: klient -> klienci/<login>, administrator (Mój dysk) -> klienci/_prywatne/<login>."""
        if client["role"] == "admin":
            return settings.clients_dir / "_prywatne" / storage.safe_slug(client["username"])
        return settings.clients_dir / storage.safe_slug(client["username"])

    # ------------------------------------------------------------------ katalogi, kosz, wersje

    def folder_row(owner_id: int, folder_id: int | None):
        if not folder_id:
            return None
        f = db.one("SELECT * FROM folders WHERE id = ? AND owner_id = ?", folder_id, owner_id)
        if not f:
            raise HTTPException(404, "Nie ma takiego katalogu.")
        return f

    def folder_chain(folder) -> list:
        chain = []
        while folder is not None:
            chain.insert(0, folder)
            folder = db.one("SELECT * FROM folders WHERE id = ?", folder["parent_id"]) if folder["parent_id"] else None
            if len(chain) > 32:
                break
        return chain

    def folder_dir(owner, folder) -> Path:
        base = client_dir(owner)
        for f in folder_chain(folder):
            base = base / storage.safe_name(f["name"], "katalog")
        return base

    def subtree_ids(folder_id: int) -> list[int]:
        ids, todo = [], [folder_id]
        while todo:
            fid = todo.pop()
            ids.append(fid)
            todo += [r["id"] for r in db.all("SELECT id FROM folders WHERE parent_id = ?", fid)]
        return ids

    def all_folders(owner_id: int) -> list[dict]:
        """Wszystkie katalogi przestrzeni z pełną ścieżką (do „Przenieś do…”)."""
        rows = {r["id"]: dict(r) for r in db.all("SELECT * FROM folders WHERE owner_id = ?", owner_id)}

        def path(r):
            parts, cur, guard = [], r, 0
            while cur and guard < 32:
                parts.insert(0, cur["name"])
                cur, guard = rows.get(cur["parent_id"]), guard + 1
            return " / ".join(parts)
        out = [{"id": r["id"], "path": path(r)} for r in rows.values()]
        return sorted(out, key=lambda r: r["path"].lower())

    def trash_file(f, user) -> None:
        """Do kosza: plik fizycznie trafia do kosz/, wiersz zostaje (można przywrócić przez 30 dni)."""
        src = settings.clients_dir / f["rel_path"]
        dest = settings.trash_dir / f"{f['id']}_{storage.safe_name(f['original_name'])}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            shutil.move(str(src), dest)
        db.run("UPDATE files SET deleted_at = ?, deleted_by = ?, trash_path = ? WHERE id = ?",
               now_iso(), user["id"], dest.name, f["id"])
        db.log(user["id"], "trash", f["rel_path"])

    def restore_file(f, user) -> None:
        owner = db.one("SELECT * FROM users WHERE id = ?", f["client_id"])
        folder = db.one("SELECT * FROM folders WHERE id = ?", f["folder_id"]) if f["folder_id"] else None
        target = settings.clients_dir / f["rel_path"]
        if folder is None and f["folder_id"]:
            target = client_dir(owner) / Path(f["rel_path"]).name  # katalog już nie istnieje
        if target.exists():
            target = storage.unique_path(target.parent, target.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        src = settings.trash_dir / (f["trash_path"] or "")
        if src.is_file():
            shutil.move(str(src), target)
        db.run("UPDATE files SET deleted_at = NULL, deleted_by = NULL, trash_path = NULL, rel_path = ?, folder_id = ?"
               " WHERE id = ?", target.relative_to(settings.clients_dir).as_posix(),
               f["folder_id"] if folder else None, f["id"])
        db.log(user["id"], "restore", f["original_name"])

    def purge_file(f) -> None:
        if f["trash_path"]:
            (settings.trash_dir / f["trash_path"]).unlink(missing_ok=True)
        for v in db.all("SELECT rel_path FROM file_versions WHERE file_id = ?", f["id"]):
            (settings.clients_dir / v["rel_path"]).unlink(missing_ok=True)
        thumbs.remove(settings.thumbs_dir, f["id"])
        db.run("DELETE FROM files WHERE id = ?", f["id"])

    def space_url(user, owner_id: int, folder_id: int | None = None) -> str:
        """Adres przestrzeni w panelu (klient / karta klienta u admina / Mój dysk)."""
        if user["role"] == "admin":
            base = "/admin/my" if owner_id == user["id"] else f"/admin/clients/{owner_id}"
        else:
            base = "/files"
        return base + (f"?folder={folder_id}" if folder_id else "") + "#pliki"

    def purge_old_trash() -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=TRASH_DAYS)).isoformat()
        old = db.all("SELECT * FROM files WHERE deleted_at IS NOT NULL AND deleted_at < ?", cutoff)
        for f in old:
            purge_file(f)
        return len(old)

    def client_usage(client_id: int) -> int:
        """Miejsce zajęte przez pliki wysłane przez samego klienta (to, co udostępnia admin, się nie liczy)."""
        row = db.one(
            "SELECT COALESCE(SUM(f.size), 0) AS used FROM files f JOIN users u ON u.id = f.uploaded_by"
            " WHERE f.client_id = ? AND u.role = 'client' AND f.deleted_at IS NULL", client_id,
        )
        return row["used"]

    def quota_info(client) -> dict:
        used = client_usage(client["id"])
        limit = (client["quota_mb"] or 0) * 1024 * 1024
        return {"used": used, "limit": limit, "percent": min(100, round(used * 100 / limit)) if limit else 0}

    class ChunkedFile:
        """Plik złożony z kawałków, gotowy do przeniesienia na miejsce docelowe."""

        def __init__(self, row):
            self.row = row
            self.filename = row["name"]
            self.subdir = row["target"] or ""  # podfolder przy wysyłce całego folderu, np. „Grecja 2021/Dzień 1”

    SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

    def clean_dir(raw: str) -> str:
        """Bezpieczna ścieżka podfolderu z przeglądarki (pusta = bez podfolderu)."""
        raw = raw.strip().strip("/\\")
        return storage.safe_rel_path(raw) if raw else ""

    def ensure_folder_path(owner, base_folder_id: int | None, subdir: str, creator) -> int | None:
        """Zakłada brakujące katalogi z wysłanego folderu i zwraca id najgłębszego."""
        parent = folder_row(owner["id"], base_folder_id)
        parent_id = parent["id"] if parent else None
        for part in [p for p in subdir.split("/") if p][:20]:
            name = storage.safe_name(part, "katalog")[:80]
            existing = next((r for r in db.all("SELECT * FROM folders WHERE owner_id = ? AND parent_id IS ?",
                                               owner["id"], parent_id) if r["name"].casefold() == name.casefold()), None)
            if existing:
                parent_id = existing["id"]
                continue
            parent_id = db.run("INSERT INTO folders (owner_id, parent_id, name, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                               owner["id"], parent_id, name, creator["id"], now_iso())
            folder_dir(owner, db.one("SELECT * FROM folders WHERE id = ?", parent_id)).mkdir(parents=True, exist_ok=True)
        return parent_id

    def find_folder_path(owner, base_folder_id: int | None, subdir: str) -> tuple[bool, int | None]:
        """Jak ensure_folder_path, ale bez zakładania: (czy istnieje, id katalogu)."""
        parent_id = base_folder_id
        for part in [p for p in subdir.split("/") if p][:20]:
            name = storage.safe_name(part, "katalog")[:80]
            found = next((r for r in db.all("SELECT id, name FROM folders WHERE owner_id = ? AND parent_id IS ?",
                                            owner["id"], parent_id) if r["name"].casefold() == name.casefold()), None)
            if not found:
                return False, None
            parent_id = found["id"]
        return True, parent_id

    def resolve_dest(user, dest: dict):
        """Dokąd trafia plik wysyłany z przeglądarki: (właściciel przestrzeni, katalog, notatka, do akceptacji)."""
        kind = str(dest.get("kind", ""))
        try:
            folder_id = int(dest["folder_id"]) if dest.get("folder_id") not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            raise HTTPException(400, "Nieprawidłowy katalog.")
        if kind == "my" and user["role"] == "admin":
            owner = user
        elif kind == "client" and user["role"] == "admin":
            owner = client_or_404(int(dest.get("client_id") or 0))
        elif kind == "self" and user["role"] == "client":
            owner = space_of(user)
        else:
            raise HTTPException(403, "Brak dostępu do tej przestrzeni.")
        folder_row(owner["id"], folder_id)  # sprawdza, czy katalog należy do przestrzeni
        return owner, folder_id, str(dest.get("note", ""))[:500], bool(dest.get("review")) and user["role"] == "admin"

    def finalize_upload(user, row) -> int | None:
        """Ostatni kawałek dotarł: plik od razu trafia na swoje miejsce (wysyłka przerwana później nic nie traci)."""
        dest = json.loads(row["dest"])
        owner, folder_id, note, review = resolve_dest(user, dest)
        src = ChunkedFile(row)
        fid = save_into(owner, user, src, "", folder_id, dest.get("mtime"))
        if fid and review:
            db.run("UPDATE files SET review_status = 'review', review_by = NULL, review_at = NULL WHERE id = ?", fid)
        return fid

    def save_into(owner, uploader, src, note: str, folder_id: int | None, src_mtime: float | None = None) -> int | None:
        """Zapis z uwzględnieniem podfolderu; pomija pliki systemowe (Thumbs.db, .DS_Store…)."""
        if (src.filename or "").casefold() in SKIP_NAMES:
            if isinstance(src, ChunkedFile):
                chunk_store.remove(src.row["id"])
            return None
        sub = getattr(src, "subdir", "")
        target = ensure_folder_path(owner, folder_id, sub, uploader) if sub else folder_id
        return save_upload(owner, uploader, src, note, target, src_mtime)

    def incoming(user, files, upload_ids) -> list:
        out = [f for f in (files or []) if f.filename]
        for uid in upload_ids or []:
            row = chunk_store.take_done(uid, user["id"])
            if not row:
                raise HTTPException(400, "Wysyłka pliku nie została dokończona — spróbuj ponownie.")
            out.append(ChunkedFile(row))
        return out

    def place(source, dest: Path, limit: int) -> tuple[int, str]:
        if isinstance(source, ChunkedFile):
            row = source.row
            if row["size"] > limit:
                chunk_store.remove(row["id"])
                raise storage.TooLarge()
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(chunk_store.path(row["id"]), dest)
            chunk_store.remove(row["id"])
            return row["size"], row["sha256"]
        return storage.copy_hashed(source.file, dest, settings.tmp_dir, limit)

    def save_upload(client, uploader, upload, note: str, folder_id: int | None = None, src_mtime: float | None = None) -> int:
        """Zapis pliku w przestrzeni (klienta lub Mój dysk). Ta sama nazwa w tym samym katalogu = nowa wersja."""
        limit = max_upload
        quota_exceeded = False
        if uploader["role"] == "client" and client["quota_mb"]:
            remaining = client["quota_mb"] * 1024 * 1024 - client_usage(client["id"])
            if remaining < limit:
                limit, quota_exceeded = max(0, remaining), True
        folder = folder_row(client["id"], folder_id)
        name = storage.safe_name(upload.filename or "plik")
        wanted = (upload.filename or name).casefold()  # casefold: także polskie znaki (SQLite lower() zna tylko ASCII)
        existing = next((r for r in db.all(
            "SELECT * FROM files WHERE client_id = ? AND deleted_at IS NULL AND folder_id IS ?",
            client["id"], folder["id"] if folder else None) if r["original_name"].casefold() == wanted), None)
        if folder:
            target_dir = folder_dir(client, folder)
        else:
            target_dir = client_dir(client) / ("od BSK Media" if uploader["role"] == "admin" and client["role"] == "client" else "")
        tmp_dest = storage.unique_path(settings.tmp_dir, f"in-{secrets.token_hex(6)}")
        try:
            size, sha = place(upload, tmp_dest, limit)
        except storage.TooLarge:
            if quota_exceeded:
                raise HTTPException(
                    413, f"Brak miejsca na koncie (limit {client['quota_mb']} MB). Skontaktuj się z BSK Media."
                )
            raise HTTPException(413, f"Plik przekracza limit {settings.max_upload_mb} MB.")
        if existing and (settings.clients_dir / existing["rel_path"]).is_file():
            # nowa wersja istniejącego pliku: poprzednia trafia do .wersje/, wiersz (i linki) zostają
            cur = settings.clients_dir / existing["rel_path"]
            vdir = cur.parent / ".wersje"
            vdir.mkdir(parents=True, exist_ok=True)
            stem, dot, ext = cur.name.rpartition(".")
            old = vdir / (f"{stem}.v{existing['version']}.{ext}" if dot else f"{cur.name}.v{existing['version']}")
            old = storage.unique_path(vdir, old.name)
            shutil.move(str(cur), old)
            shutil.move(str(tmp_dest), cur)
            db.run("INSERT INTO file_versions (file_id, version, rel_path, size, sha256, uploaded_by, uploaded_at, note)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", existing["id"], existing["version"],
                   old.relative_to(settings.clients_dir).as_posix(), existing["size"], existing["sha256"],
                   existing["uploaded_by"], existing["uploaded_at"], existing["note"])
            reset = "review" if existing["review_status"] in ("approved", "changes", "review") else existing["review_status"]
            db.run("UPDATE files SET size = ?, sha256 = ?, uploaded_by = ?, uploaded_at = ?, note = ?, version = version + 1,"
                   " review_status = ?, review_by = NULL, review_at = NULL, src_mtime = ? WHERE id = ?",
                   size, sha, uploader["id"], now_iso(), note[:500], reset, src_mtime, existing["id"])
            db.log(uploader["id"], "upload_version", f"{existing['rel_path']} v{existing['version'] + 1}")
            thumbs.generate_async(cur, settings.thumbs_dir, existing["id"], sha)
            return existing["id"]
        dest = storage.unique_path(target_dir, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_dest), dest)
        rel = dest.relative_to(settings.clients_dir).as_posix()
        fid = db.run(
            "INSERT INTO files (client_id, rel_path, original_name, size, sha256, uploaded_by, uploaded_at, note, folder_id, src_mtime)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            client["id"], rel, upload.filename or name, size, sha, uploader["id"], now_iso(), note[:500],
            folder["id"] if folder else None, src_mtime,
        )
        db.log(uploader["id"], "upload", f"{rel} ({size} B)")
        thumbs.generate_async(dest, settings.thumbs_dir, fid, sha)
        return fid

    def file_for(user, file_id: int):
        f = db.one("SELECT * FROM files WHERE id = ? AND deleted_at IS NULL", file_id)
        if not f or (user["role"] != "admin" and f["client_id"] != sid(user)):
            raise HTTPException(404, "Nie znaleziono pliku.")
        if user["role"] == "admin" and f["client_id"] != user["id"]:
            owner = db.one("SELECT role FROM users WHERE id = ?", f["client_id"])
            if owner and owner["role"] == "admin":
                raise HTTPException(404, "Nie znaleziono pliku.")  # prywatny dysk innego administratora
        path = settings.clients_dir / f["rel_path"]
        if not storage.inside(settings.clients_dir, path) or not path.is_file():
            raise HTTPException(404, "Plik nie istnieje na dysku.")
        return f, path

    def add_message(client_id: int, author, body: str) -> None:
        body = body.strip()
        if not body:
            return
        db.run(
            "INSERT INTO messages (client_id, author_id, body, created_at) VALUES (?, ?, ?, ?)",
            client_id, author["id"], body[:5000], now_iso(),
        )

    def mark_seen(user, client_id: int) -> None:
        db.run("INSERT INTO seen (user_id, client_id, seen_at) VALUES (?, ?, ?) ON CONFLICT (user_id, client_id)"
               " DO UPDATE SET seen_at = excluded.seen_at", user["id"], client_id, now_iso())

    def unread_by_client(admin_id: int) -> dict[int, int]:
        """Dla administratora: nowe wiadomości i pliki od klientów, których jeszcze nie oglądał."""
        rows = db.all(
            "SELECT x.client_id, COUNT(*) AS n FROM ("
            " SELECT m.client_id, m.created_at AS ts FROM messages m JOIN users a ON a.id = m.author_id WHERE a.role = 'client'"
            " UNION ALL SELECT f.client_id, f.uploaded_at FROM files f JOIN users up ON up.id = f.uploaded_by"
            " WHERE up.role = 'client' AND f.deleted_at IS NULL) x"
            " LEFT JOIN seen s ON s.client_id = x.client_id AND s.user_id = ?"
            " WHERE s.seen_at IS NULL OR x.ts > s.seen_at GROUP BY x.client_id",
            admin_id,
        )
        return {r["client_id"]: r["n"] for r in rows}

    def unread_for(user) -> int:
        if user["role"] == "admin":
            return sum(unread_by_client(user["id"]).values())
        space_id = sid(user)
        seen = db.one("SELECT seen_at FROM seen WHERE user_id = ? AND client_id = ?", user["id"], space_id)
        since = seen["seen_at"] if seen else ""
        return db.one(
            "SELECT (SELECT COUNT(*) FROM messages m JOIN users a ON a.id = m.author_id"
            "        WHERE m.client_id = ? AND a.role = 'admin' AND m.created_at > ?)"
            " + (SELECT COUNT(*) FROM files f WHERE f.client_id = ? AND f.uploaded_by != ? AND f.uploaded_at > ?"
            "    AND f.deleted_at IS NULL) AS n",
            space_id, since, space_id, user["id"], since,
        )["n"]

    def message_json(m, user) -> dict:
        name = m["display_name"] or m["username"]
        return {"id": m["id"], "mine": m["author_id"] == user["id"], "author": name, "initials": _initials(name),
                "agency": m["role"] == "admin", "time": _local_dt(m["created_at"]), "body": m["body"]}

    def notify_client(client, subject: str, lines: list[str]) -> None:
        """E-mail do klienta i osób z jego firmy (każda z podanym e-mailem i włączonymi powiadomieniami)."""
        for person in [client, *space_members(client["id"])]:
            if person["email"] and person["notify_email"] and person["active"]:
                notify.send(settings, person["email"], subject, lines, settings.public_url + "/files",
                            "Otwórz strefę klienta", throttle_key=f"client:{person['id']}")

    def thread(client_id: int):
        return db.all(
            "SELECT m.*, u.display_name, u.username, u.role FROM messages m"
            " JOIN users u ON u.id = m.author_id WHERE m.client_id = ? ORDER BY m.created_at, m.id",
            client_id,
        )

    def client_files(client_id: int, folder_id: int | None | str = "all"):
        """Pliki przestrzeni: wszystkie (folder_id='all'), z katalogu głównego (None) albo z danego katalogu."""
        sql = ("SELECT f.*, u.role AS uploader_role, u.display_name AS uploader_name,"
               " (SELECT COUNT(*) FROM annotations a WHERE a.file_id = f.id AND a.resolved = 0) AS open_comments"
               " FROM files f JOIN users u ON u.id = f.uploaded_by WHERE f.client_id = ? AND f.deleted_at IS NULL")
        args: list = [client_id]
        if folder_id is None:
            sql += " AND f.folder_id IS NULL"
        elif folder_id != "all":
            sql += " AND f.folder_id = ?"
            args.append(folder_id)
        return db.all(sql + " ORDER BY f.uploaded_at DESC, f.id DESC", *args)

    # ------------------------------------------------------------------ statystyki i dyski

    def parse_quota(gb: str | None, mb: int) -> int:
        """Limit z formularza: pole w GB (np. „1024” = 1 TB) albo starsze w MB. 0 = bez limitu."""
        if gb not in (None, ""):
            try:
                return max(0, round(float(str(gb).replace(",", ".")) * 1024))
            except ValueError:
                raise HTTPException(400, "Limit miejsca musi być liczbą.")
        return max(0, mb)

    def daily_files(client_id: int | None = None, days: int = 30) -> list[dict]:
        start = (date.today() - timedelta(days=days - 1)).isoformat()
        sql = ("SELECT substr(f.uploaded_at, 1, 10) AS d,"
               " SUM(CASE WHEN u.role = 'client' THEN 1 ELSE 0 END) AS c,"
               " SUM(CASE WHEN u.role = 'admin' THEN 1 ELSE 0 END) AS a"
               " FROM files f JOIN users u ON u.id = f.uploaded_by JOIN users c ON c.id = f.client_id"
               " WHERE c.role = 'client' AND substr(f.uploaded_at, 1, 10) >= ?")
        args: list = [start]
        if client_id is not None:
            sql += " AND f.client_id = ?"
            args.append(client_id)
        found = {r["d"]: r for r in db.all(sql + " GROUP BY d", *args)}
        return [{"date": d, "client": (found.get(d.isoformat()) or {"c": 0})["c"],
                 "agency": (found.get(d.isoformat()) or {"a": 0})["a"]} for d in charts.day_range(days)]

    def daily_backups(device_id: int, days: int = 30) -> list[dict]:
        start = (date.today() - timedelta(days=days - 1)).isoformat()
        found = {r["d"]: r["n"] for r in db.all(
            "SELECT substr(uploaded_at, 1, 10) AS d, COUNT(*) AS n FROM backup_files"
            " WHERE device_id = ? AND substr(uploaded_at, 1, 10) >= ? GROUP BY d", device_id, start)}
        return [{"date": d, "files": found.get(d.isoformat(), 0)} for d in charts.day_range(days)]

    def disks() -> list[dict]:
        """Zajętość dysków NAS widzianych przez portal (bez powtórzeń tego samego dysku)."""
        out, seen = [], {}
        for label, path in (("Dane klientów i kopie (HDD)", settings.data_dir),
                            ("Portal i baza danych (SSD)", settings.db_path.parent),
                            ("Kopia zapasowa (SSD)", settings.backup_dir)):
            if not path or not Path(path).exists():
                continue
            dev = os.stat(path).st_dev
            if dev in seen:
                seen[dev]["label"] += " · " + label
                continue
            u = shutil.disk_usage(path)
            seen[dev] = {"label": label, "used": u.total - u.free, "total": u.total, "free": u.free}
            out.append(seen[dev])
        return out

    def device_rows():
        return db.all(
            "SELECT d.*, (SELECT COUNT(*) FROM backup_files b WHERE b.device_id = d.id) AS file_count,"
            " (SELECT COALESCE(SUM(size), 0) FROM backup_files b WHERE b.device_id = d.id) AS total_size,"
            " (SELECT MAX(uploaded_at) FROM backup_files b WHERE b.device_id = d.id) AS last_backup"
            " FROM devices d ORDER BY d.active DESC, d.employee, d.name"
        )

    def client_rows():
        return db.all(
            "SELECT u.*, (SELECT COUNT(*) FROM files f WHERE f.client_id = u.id AND f.deleted_at IS NULL) AS file_count,"
            " (SELECT COALESCE(SUM(size), 0) FROM files f WHERE f.client_id = u.id AND f.deleted_at IS NULL) AS total_size,"
            " (SELECT COALESCE(SUM(f.size), 0) FROM files f JOIN users x ON x.id = f.uploaded_by WHERE f.client_id = u.id AND x.role = 'client' AND f.deleted_at IS NULL) AS own_size,"
            " (SELECT MAX(uploaded_at) FROM files f WHERE f.client_id = u.id AND f.deleted_at IS NULL) AS last_upload,"
            " (SELECT MAX(created_at) FROM messages m WHERE m.client_id = u.id) AS last_message"
            " FROM users u WHERE u.role = 'client' AND u.space_id IS NULL ORDER BY u.active DESC, u.company, u.display_name"
        )

    def online(last_seen: str | None, hours: int = 2) -> bool:
        if not last_seen:
            return False
        try:
            return datetime.now(timezone.utc) - datetime.fromisoformat(last_seen) < timedelta(hours=hours)
        except ValueError:
            return False

    templates.env.globals["online"] = online

    PAGE = 300  # tyle plików na raz w galerii — tysiące kart naraz zawieszają przeglądarkę

    def space_ctx(owner, folder_id: int | None, show: int = PAGE) -> dict:
        """Dane do przeglądarki plików: bieżący katalog, okruszki, podkatalogi z okładkami, pliki (porcjami)."""
        folder = folder_row(owner["id"], folder_id) if folder_id else None
        subs = []
        for d in db.all(
            "SELECT d.*, (SELECT COUNT(*) FROM files x WHERE x.folder_id = d.id AND x.deleted_at IS NULL) AS n_files,"
            " (SELECT COUNT(*) FROM folders y WHERE y.parent_id = d.id) AS n_dirs"
            " FROM folders d WHERE d.owner_id = ? AND d.parent_id IS ? ORDER BY lower(d.name)",
            owner["id"], folder["id"] if folder else None,
        ):
            recent = db.all("SELECT id, original_name, sha256 FROM files WHERE folder_id = ? AND deleted_at IS NULL"
                            " ORDER BY uploaded_at DESC LIMIT 30", d["id"])
            covers = [r for r in recent if thumbs.can_thumb(r["original_name"])][:3]
            subs.append({**dict(d), "covers": covers})
        files = client_files(owner["id"], folder["id"] if folder else None)
        show = max(PAGE, min(int(show or PAGE), 100_000))
        return {
            "owner": owner, "folder": folder, "crumbs": folder_chain(folder) if folder else [],
            "subfolders": subs, "files": files[:show], "files_total": len(files), "show_next": show + PAGE,
            "all_folders": all_folders(owner["id"]),
        }

    templates.env.globals["thumb_kind"] = thumbs.kind
    templates.env.globals["can_thumb"] = thumbs.can_thumb

    # ------------------------------------------------------------------ aplikacja mobilna (PWA)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return JSONResponse({
            "name": settings.site_name, "short_name": "BSK Cloud", "lang": "pl",
            "description": "Pliki, galeria i wiadomości BSK Media",
            "start_url": "/?source=app", "scope": "/", "display": "standalone",
            "background_color": "#ffffff", "theme_color": "#0f1729", "orientation": "any",
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/static/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
            "shortcuts": [
                {"name": "Mój dysk", "url": "/admin/my", "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}]},
                {"name": "Szukaj", "url": "/search", "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}]},
            ],
        }, media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        """Service worker z zakresem całej strony; wersja = skrót plików statycznych (nowa wersja po aktualizacji)."""
        code = (BASE / "static" / "sw.js").read_text(encoding="utf-8").replace("__VERSION__", templates.env.globals["asset_v"])
        return Response(code, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/offline", response_class=HTMLResponse, include_in_schema=False)
    def offline(request: Request):
        return render(request, "offline.html", user=None)

    # ------------------------------------------------------------------ public

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", 303)
        return RedirectResponse("/admin" if user["role"] == "admin" else "/files", 303)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if current_user(request):
            return RedirectResponse("/", 303)
        return render(request, "login.html")

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...), remember: str = Form(""),
              csrf: str = Form("")):
        check_csrf(request, csrf)
        ip = client_ip(request)
        username = username.strip()
        if limiter.blocked(username, ip):
            flash(request, "Zbyt wiele nieudanych prób. Spróbuj ponownie za 15 minut.", "error")
            return RedirectResponse("/login", 303)
        user = db.one("SELECT * FROM users WHERE username = ?", username)
        if user is None:
            security.verify_dummy()
        if not user or not user["active"] or not security.verify_password(password, user["password_hash"]):
            limiter.fail(username, ip)
            db.log(None, "login_failed", f"{username} z {ip}")
            flash(request, "Nieprawidłowy login lub hasło.", "error")
            return RedirectResponse("/login", 303)
        limiter.success(username)
        request.session.clear()
        ttl = REMEMBER_DAYS * 86400 if remember else settings.session_hours * 3600
        request.session.update(uid=user["id"], sv=user["session_version"], csrf=security.new_csrf(),
                               exp=int(time.time() + ttl))
        db.run("UPDATE users SET last_login = ? WHERE id = ?", now_iso(), user["id"])
        db.log(user["id"], "login", ip)
        return RedirectResponse("/", 303)

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        check_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/login", 303)

    @app.get("/account", response_class=HTMLResponse)
    def account(request: Request):
        user = require_user(request, allow_pw_change=True)
        return render(request, "account.html", user=user, nav="account", smtp_ok=notify.configured(settings),
                      notify_to=settings.notify_email, smtp_host=settings.smtp_host)

    @app.post("/account/password")
    def change_password(
        request: Request,
        current: str = Form(...),
        new: str = Form(...),
        repeat: str = Form(...),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        user = require_user(request, allow_pw_change=True)
        if not security.verify_password(current, user["password_hash"]):
            flash(request, "Obecne hasło jest nieprawidłowe.", "error")
        elif new != repeat:
            flash(request, "Nowe hasła nie są takie same.", "error")
        elif problem := security.password_problem(new):
            flash(request, problem, "error")
        else:
            db.run(
                "UPDATE users SET password_hash = ?, must_change_password = 0,"
                " session_version = session_version + 1 WHERE id = ?",
                security.hash_password(new), user["id"],
            )
            request.session["sv"] = user["session_version"] + 1
            db.log(user["id"], "password_changed")
            flash(request, "Hasło zostało zmienione.")
            return RedirectResponse("/", 303)
        return RedirectResponse("/account", 303)

    # ------------------------------------------------------------------ klient

    @app.get("/files", response_class=HTMLResponse)
    def my_files(request: Request, folder: int | None = None, show: int = 0):
        user = require_user(request, "client")
        owner = space_of(user)
        files = client_files(owner["id"])
        space = space_ctx(owner, folder, show)
        unread = unread_for(user)
        mark_seen(user, owner["id"])
        sent = [f for f in files if f["uploader_role"] == "client"]
        received = [f for f in files if f["uploader_role"] != "client"]
        return render(
            request, "client_files.html", files=files, messages=thread(owner["id"]), quota=quota_info(owner),
            days=daily_files(owner["id"]), sent=sent, received=received, nav="files", owner=owner,
            members=space_members(owner["id"]),
            sent_size=sum(f["size"] for f in sent), received_size=sum(f["size"] for f in received),
            new_count=unread, unread_total=0, space=space, base_url="/files",
            upload_action="/files/upload",
        )

    @app.post("/files/upload")
    def client_upload(
        request: Request,
        files: list[UploadFile] | None = File(None),
        upload_ids: list[str] = Form([]),
        note: str = Form(""),
        folder_id: int | None = Form(None),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        user = require_user(request, "client")
        owner = space_of(user)
        names = []
        for up in incoming(user, files, upload_ids):
            if save_into(owner, user, up, note, folder_id):
                names.append(up.filename)
        if note.strip():
            add_message(owner["id"], user, note)
        if names:
            flash(request, f"Wysłano plików: {len(names)}.")
            notify_admin(
                settings,
                f"Nowe pliki od {owner['company'] or user['display_name'] or user['username']}",
                f"{user['display_name'] or user['username']} wysłał(a) pliki: " + ", ".join(names)
                + (f"\n\nKomentarz: {note}" if note.strip() else ""),
                link=f"{settings.public_url}/admin/clients/{owner['id']}", throttle_key=f"admin:{owner['id']}",
            )
        return RedirectResponse(space_url(user, owner["id"], folder_id), 303)

    @app.post("/files/message")
    def client_message(request: Request, body: str = Form(...), csrf: str = Form("")):
        check_csrf(request, csrf)
        user = require_user(request, "client")
        owner = space_of(user)
        if body.strip():
            add_message(owner["id"], user, body)
            mark_seen(user, owner["id"])
            notify_admin(
                settings,
                f"Wiadomość od {owner['company'] or user['display_name'] or user['username']}",
                f"{user['display_name'] or user['username']}: " + body.strip()[:1500],
                link=f"{settings.public_url}/admin/clients/{owner['id']}", throttle_key=f"admin:{owner['id']}",
            )
        return RedirectResponse("/files#wiadomosci", 303)

    @app.get("/files/{file_id}/download")
    def download(request: Request, file_id: int):
        user = require_user(request)
        f, path = file_for(user, file_id)
        return FileResponse(path, filename=f["original_name"])

    @app.post("/files/{file_id}/delete")
    def delete_file(request: Request, file_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        user = require_user(request)
        f, path = file_for(user, file_id)
        if user["role"] != "admin" and f["uploaded_by"] != user["id"]:
            raise HTTPException(403, "Możesz usuwać tylko pliki wysłane przez siebie.")
        trash_file(f, user)
        flash(request, f"Plik {f['original_name']} trafił do kosza"
              + (" — możesz go przywrócić w zakładce Kosz." if user["role"] == "admin" else "."))
        return RedirectResponse(space_url(user, f["client_id"], f["folder_id"]), 303)

    # ------------------------------------------------------------------ Mój dysk, katalogi, galeria, kosz, wyszukiwarka

    @app.get("/admin/my", response_class=HTMLResponse)
    def my_drive(request: Request, folder: int | None = None, show: int = 0):
        admin = require_user(request, "admin")
        space = space_ctx(admin, folder, show)
        used = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM files WHERE client_id = ?"
                      " AND deleted_at IS NULL", admin["id"])
        return render(request, "my_drive.html", space=space, base_url="/admin/my", upload_action="/admin/my/upload",
                      used=used, nav="my", folder_path=client_dir(admin).as_posix().replace(settings.data_dir.as_posix(), "", 1))

    @app.post("/admin/my/upload")
    def my_upload(
        request: Request,
        files: list[UploadFile] | None = File(None),
        upload_ids: list[str] = Form([]),
        note: str = Form(""),
        folder_id: int | None = Form(None),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        count = 0
        for up in incoming(admin, files, upload_ids):
            if save_into(admin, admin, up, note, folder_id):
                count += 1
        flash(request, f"Zapisano plików na Twoim dysku: {count}.")
        return RedirectResponse(space_url(admin, admin["id"], folder_id), 303)

    def space_owner_for_admin(admin, owner_id: int):
        owner = db.one("SELECT * FROM users WHERE id = ?", owner_id)
        if not owner or (owner["role"] == "admin" and owner["id"] != admin["id"]):
            raise HTTPException(404, "Nie ma takiej przestrzeni.")
        return owner

    @app.post("/spaces/{owner_id}/folders")
    def create_folder(request: Request, owner_id: int, name: str = Form(...), parent_id: int | None = Form(None),
                      csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        owner = space_owner_for_admin(admin, owner_id)
        parent = folder_row(owner_id, parent_id)
        clean = storage.safe_name(name.strip(), "")[:80]
        if not clean or clean.startswith("."):
            flash(request, "Podaj nazwę katalogu.", "error")
        elif any(r["name"].casefold() == clean.casefold() for r in db.all(
                "SELECT name FROM folders WHERE owner_id = ? AND parent_id IS ?", owner_id, parent["id"] if parent else None)):
            flash(request, f"Katalog „{clean}” już istnieje.", "error")
        else:
            fid = db.run("INSERT INTO folders (owner_id, parent_id, name, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                         owner_id, parent["id"] if parent else None, clean, admin["id"], now_iso())
            folder_dir(owner, db.one("SELECT * FROM folders WHERE id = ?", fid)).mkdir(parents=True, exist_ok=True)
            db.log(admin["id"], "folder_created", clean)
            flash(request, f"Utworzono katalog „{clean}”.")
        return RedirectResponse(space_url(admin, owner_id, parent["id"] if parent else None), 303)

    @app.post("/folders/{folder_id}/rename")
    def rename_folder(request: Request, folder_id: int, name: str = Form(...), csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        folder = db.one("SELECT * FROM folders WHERE id = ?", folder_id)
        if not folder:
            raise HTTPException(404, "Nie ma takiego katalogu.")
        owner = space_owner_for_admin(admin, folder["owner_id"])
        clean = storage.safe_name(name.strip(), "")[:80]
        if not clean or clean.startswith("."):
            flash(request, "Podaj nazwę katalogu.", "error")
            return RedirectResponse(space_url(admin, owner["id"], folder_id), 303)
        old_dir = folder_dir(owner, folder)
        db.run("UPDATE folders SET name = ? WHERE id = ?", clean, folder_id)
        new_dir = folder_dir(owner, db.one("SELECT * FROM folders WHERE id = ?", folder_id))
        if old_dir != new_dir:
            if new_dir.exists():
                db.run("UPDATE folders SET name = ? WHERE id = ?", folder["name"], folder_id)
                flash(request, "Na dysku istnieje już katalog o tej nazwie.", "error")
                return RedirectResponse(space_url(admin, owner["id"], folder_id), 303)
            if old_dir.exists():
                old_dir.rename(new_dir)
            old_rel = old_dir.relative_to(settings.clients_dir).as_posix() + "/"
            new_rel = new_dir.relative_to(settings.clients_dir).as_posix() + "/"
            for table in ("files", "file_versions"):
                db.run(f"UPDATE {table} SET rel_path = ? || substr(rel_path, ?) WHERE rel_path LIKE ? ESCAPE '\\'",
                       new_rel, len(old_rel) + 1, old_rel.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        db.log(admin["id"], "folder_renamed", f"{folder['name']} -> {clean}")
        flash(request, f"Zmieniono nazwę na „{clean}”.")
        return RedirectResponse(space_url(admin, owner["id"], folder_id), 303)

    @app.post("/folders/{folder_id}/delete")
    def delete_folder(request: Request, folder_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        folder = db.one("SELECT * FROM folders WHERE id = ?", folder_id)
        if not folder:
            raise HTTPException(404, "Nie ma takiego katalogu.")
        owner = space_owner_for_admin(admin, folder["owner_id"])
        ids = subtree_ids(folder_id)
        marks = ",".join("?" * len(ids))
        moved = 0
        for f in db.all(f"SELECT * FROM files WHERE folder_id IN ({marks}) AND deleted_at IS NULL", *ids):
            trash_file(f, admin)
            moved += 1
        top_dir = folder_dir(owner, folder)
        db.run(f"UPDATE files SET folder_id = NULL WHERE folder_id IN ({marks})", *ids)
        db.run(f"DELETE FROM folders WHERE id IN ({marks})", *ids)
        if top_dir.is_dir():
            shutil.rmtree(top_dir, ignore_errors=True)  # puste katalogi i stare wersje
        db.log(admin["id"], "folder_deleted", f"{folder['name']} ({moved} plików do kosza)")
        flash(request, f"Usunięto katalog „{folder['name']}”" + (f", plików w koszu: {moved}." if moved else "."))
        return RedirectResponse(space_url(admin, owner["id"], folder["parent_id"]), 303)

    @app.post("/files/bulk")
    def bulk_files(request: Request, ids: list[int] = Form([]), action: str = Form(...), folder_id: int | None = Form(None),
                   back: str = Form("/"), csrf: str = Form("")):
        """Działania na wielu plikach naraz: do kosza albo przeniesienie (przeniesienie tylko admin)."""
        check_csrf(request, csrf)
        user = require_user(request)
        if action not in ("trash", "move"):
            raise HTTPException(400, "Nieznana operacja.")
        if action == "move" and user["role"] != "admin":
            raise HTTPException(403, "Przenosić pliki może tylko BSK Media.")
        done = skipped = 0
        for fid in ids[:2000]:
            try:
                f, path = file_for(user, fid)
            except HTTPException:
                skipped += 1
                continue
            if action == "trash":
                if user["role"] != "admin" and f["uploaded_by"] != user["id"]:
                    skipped += 1
                    continue
                trash_file(f, user)
                done += 1
            elif action == "move" and user["role"] == "admin":
                owner = space_owner_for_admin(user, f["client_id"])
                target = folder_row(owner["id"], folder_id)
                if (f["folder_id"] or None) == (target["id"] if target else None):
                    continue
                dest_dir = folder_dir(owner, target) if target else client_dir(owner) / (
                    "od BSK Media" if f["uploaded_by"] != owner["id"] and owner["role"] == "client" else "")
                dest = storage.unique_path(dest_dir, path.name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), dest)
                db.run("UPDATE files SET folder_id = ?, rel_path = ? WHERE id = ?", target["id"] if target else None,
                       dest.relative_to(settings.clients_dir).as_posix(), f["id"])
                done += 1
            else:
                raise HTTPException(400, "Nieznana operacja.")
        db.log(user["id"], f"bulk_{action}", f"{done} plików")
        verb = "Przeniesiono do kosza" if action == "trash" else "Przeniesiono"
        flash(request, f"{verb}: {done} plików." + (f" Pominięto {skipped} (brak uprawnień)." if skipped else ""))
        return RedirectResponse(back if back.startswith(("/files", "/admin/")) else "/", 303)

    @app.post("/files/{file_id}/move")
    def move_file(request: Request, file_id: int, folder_id: int | None = Form(None), csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        f, path = file_for(admin, file_id)
        owner = space_owner_for_admin(admin, f["client_id"])
        target = folder_row(owner["id"], folder_id)
        if (f["folder_id"] or None) == (target["id"] if target else None):
            return RedirectResponse(space_url(admin, owner["id"], f["folder_id"]), 303)
        dest_dir = folder_dir(owner, target) if target else client_dir(owner) / (
            "od BSK Media" if f["uploaded_by"] != owner["id"] and owner["role"] == "client" else "")
        dest = storage.unique_path(dest_dir, path.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), dest)
        db.run("UPDATE files SET folder_id = ?, rel_path = ? WHERE id = ?", target["id"] if target else None,
               dest.relative_to(settings.clients_dir).as_posix(), file_id)
        db.log(admin["id"], "file_moved", f"{f['original_name']} -> {target['name'] if target else '/'}")
        flash(request, f"Przeniesiono „{f['original_name']}” do „{target['name'] if target else 'katalogu głównego'}”.")
        return RedirectResponse(space_url(admin, owner["id"], target["id"] if target else None), 303)

    @app.get("/files/{file_id}/thumb")
    def file_thumb(request: Request, file_id: int, s: str = "thumb"):
        user = require_user(request)
        f, path = file_for(user, file_id)
        size = "preview" if s == "preview" else "thumb"
        cached = thumbs.cache_path(settings.thumbs_dir, f["id"], f["sha256"], size)
        if not cached.exists():
            # bez czekania: miniatura trafia na początek kolejki, a strona spyta ponownie (202 = jeszcze nie gotowa)
            state = thumbs.request(path, settings.thumbs_dir, f["id"], f["sha256"])
            if state != "ready":
                return Response(status_code=202 if state == "pending" else 404, headers={"Cache-Control": "no-store"})
        return FileResponse(cached, media_type="image/webp",
                            headers={"Cache-Control": "private, max-age=2592000, immutable"})

    @app.get("/files/{file_id}/view")
    def file_view(request: Request, file_id: int):
        """Podgląd w przeglądarce (PDF, wideo, audio, zdjęcia) — tylko bezpieczne typy, bez HTML/SVG."""
        user = require_user(request)
        f, path = file_for(user, file_id)
        k = thumbs.kind(f["original_name"])
        ext = thumbs.ext_of(f["original_name"])
        if not (k in ("pdf", "audio") or thumbs.plays_in_browser(f["original_name"])
                or ext in ("jpg", "jpeg", "png", "webp", "gif")) or ext == "ai":
            raise HTTPException(404, "Tego pliku nie da się podejrzeć — pobierz go.")
        return FileResponse(path, filename=f["original_name"], content_disposition_type="inline", headers={
            "X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "frame-ancestors 'self'",
            "Cache-Control": "private, max-age=600"})

    @app.get("/admin/trash", response_class=HTMLResponse)
    def trash_page(request: Request):
        admin = require_user(request, "admin")
        purge_old_trash()
        rows = db.all(
            "SELECT f.*, o.username, o.company, o.display_name AS owner_name, o.role AS owner_role,"
            " d.display_name AS deleter FROM files f JOIN users o ON o.id = f.client_id"
            " LEFT JOIN users d ON d.id = f.deleted_by WHERE f.deleted_at IS NOT NULL"
            " AND (o.role = 'client' OR o.id = ?) ORDER BY f.deleted_at DESC", admin["id"])
        items = []
        for r in rows:
            try:
                left = TRASH_DAYS - (datetime.now(timezone.utc) - datetime.fromisoformat(r["deleted_at"])).days
            except ValueError:
                left = TRASH_DAYS
            items.append({**dict(r), "days_left": max(0, left)})
        return render(request, "admin_trash.html", items=items, total=sum(i["size"] for i in items),
                      trash_days=TRASH_DAYS, nav="trash")

    def trashed_or_404(admin, file_id: int):
        f = db.one("SELECT * FROM files WHERE id = ? AND deleted_at IS NOT NULL", file_id)
        if not f:
            raise HTTPException(404, "Nie ma takiego pliku w koszu.")
        space_owner_for_admin(admin, f["client_id"])
        return f

    @app.post("/admin/trash/{file_id}/restore")
    def trash_restore(request: Request, file_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        f = trashed_or_404(admin, file_id)
        restore_file(f, admin)
        flash(request, f"Przywrócono „{f['original_name']}”.")
        return RedirectResponse("/admin/trash", 303)

    @app.post("/admin/trash/{file_id}/purge")
    def trash_purge(request: Request, file_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        f = trashed_or_404(admin, file_id)
        purge_file(f)
        db.log(admin["id"], "purge", f["original_name"])
        flash(request, f"Usunięto na zawsze „{f['original_name']}”.")
        return RedirectResponse("/admin/trash", 303)

    @app.post("/admin/trash/empty")
    def trash_empty(request: Request, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        rows = db.all("SELECT f.* FROM files f JOIN users o ON o.id = f.client_id WHERE f.deleted_at IS NOT NULL"
                      " AND (o.role = 'client' OR o.id = ?)", admin["id"])
        for f in rows:
            purge_file(f)
        db.log(admin["id"], "trash_emptied", f"{len(rows)} plików")
        flash(request, f"Kosz opróżniony ({len(rows)} plików).")
        return RedirectResponse("/admin/trash", 303)

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = ""):
        user = require_user(request)
        q = q.strip()[:100]
        res = {"files": [], "folders": [], "clients": [], "backups": []}
        if len(q) >= 2:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            if user["role"] == "admin":
                scope, args = "(o.role = 'client' OR o.id = ?)", [user["id"]]
            else:
                scope, args = "o.id = ?", [sid(user)]
            res["files"] = db.all(
                "SELECT f.*, o.username, o.company, o.display_name AS owner_name, o.role AS owner_role,"
                " up.role AS uploader_role, d.name AS folder_name FROM files f JOIN users o ON o.id = f.client_id"
                " JOIN users up ON up.id = f.uploaded_by LEFT JOIN folders d ON d.id = f.folder_id"
                f" WHERE f.deleted_at IS NULL AND {scope} AND (f.original_name LIKE ? ESCAPE '\\' OR f.note LIKE ? ESCAPE '\\')"
                " ORDER BY f.uploaded_at DESC LIMIT 120", *args, like, like)
            res["folders"] = db.all(
                "SELECT d.*, o.username, o.company, o.display_name AS owner_name, o.role AS owner_role FROM folders d"
                f" JOIN users o ON o.id = d.owner_id WHERE {scope} AND d.name LIKE ? ESCAPE '\\' ORDER BY d.name LIMIT 40",
                *args, like)
            if user["role"] == "admin":
                res["clients"] = db.all(
                    "SELECT * FROM users WHERE role = 'client' AND (username LIKE ? ESCAPE '\\' OR display_name LIKE ?"
                    " ESCAPE '\\' OR company LIKE ? ESCAPE '\\' OR email LIKE ? ESCAPE '\\') LIMIT 20", like, like, like, like)
                res["backups"] = db.all(
                    "SELECT b.*, d.name AS device_name, d.employee FROM backup_files b JOIN devices d ON d.id = b.device_id"
                    " WHERE b.rel_path LIKE ? ESCAPE '\\' ORDER BY b.uploaded_at DESC LIMIT 60", like)
        total = sum(len(v) for v in res.values())
        return render(request, "search.html", q=q, res=res, total=total,
                      nav="search")

    # ------------------------------------------------------------------ import z NAS (duże archiwa kopiowane przez sieć lokalną)

    import_lock = threading.Lock()

    def import_entries() -> list[dict]:
        """Zawartość folderu bsk-dane/import (pierwszy poziom) z liczbą plików i rozmiarem."""
        base = settings.import_dir
        base.mkdir(parents=True, exist_ok=True)
        out = []
        for p in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if p.name.startswith((".", "@", "#")) or p.name.casefold() in SKIP_NAMES:
                continue
            files = size = 0
            if p.is_file():
                files, size = 1, p.stat().st_size
            else:
                for dp, dn, fn in os.walk(p):
                    dn[:] = [d for d in dn if not d.startswith((".", "@", "#"))]
                    for f in fn:
                        if f.casefold() in SKIP_NAMES or f.startswith("."):
                            continue
                        try:
                            size += os.stat(os.path.join(dp, f)).st_size
                            files += 1
                        except OSError:
                            pass
            out.append({"name": p.name, "dir": p.is_dir(), "files": files, "size": size})
        return out

    def run_import(job_id: int, owner, folder_id: int | None, names: list[str], admin) -> None:
        """Przenosi pliki z bsk-dane/import do przestrzeni (ten sam dysk = błyskawicznie, bez kopiowania)."""
        base = settings.import_dir
        try:
            todo = []
            for name in names:
                src = base / name
                if not storage.inside(base, src) or not src.exists():
                    continue
                if src.is_file():
                    todo.append((src, ""))
                    continue
                for dp, dn, fn in os.walk(src):
                    dn[:] = [d for d in dn if not d.startswith((".", "@", "#"))]
                    rel_dir = Path(dp).relative_to(base).as_posix()
                    for f in fn:
                        if f.casefold() in SKIP_NAMES or f.startswith("."):
                            continue
                        todo.append((Path(dp) / f, rel_dir))
            total = sum(p.stat().st_size for p, _ in todo if p.exists())
            db.run("UPDATE import_jobs SET total_files = ?, total_bytes = ? WHERE id = ?", len(todo), total, job_id)
            done = done_bytes = skipped = 0
            folders_cache: dict[str, int | None] = {}
            for path, rel_dir in todo:
                try:
                    st = path.stat()
                except OSError:
                    continue
                if rel_dir not in folders_cache:
                    folders_cache[rel_dir] = ensure_folder_path(owner, folder_id, rel_dir, admin) if rel_dir else folder_id
                fid = folders_cache[rel_dir]
                folder = db.one("SELECT * FROM folders WHERE id = ?", fid) if fid else None
                target_dir = folder_dir(owner, folder) if folder else client_dir(owner)
                same = db.one("SELECT id FROM files WHERE client_id = ? AND folder_id IS ? AND deleted_at IS NULL"
                              " AND original_name = ? AND size = ?", owner["id"], fid, path.name, st.st_size)
                if same:
                    skipped += 1  # już zaimportowany — zostaje w folderze import, nic nie nadpisujemy
                else:
                    dest = storage.unique_path(target_dir, storage.safe_name(path.name))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), dest)
                    # odcisk zamiast pełnej sumy: czytanie setek GB tylko po to trwałoby godzinami
                    fp = "q" + hashlib.sha256(f"{st.st_size}:{st.st_mtime_ns}:{dest}".encode()).hexdigest()[1:]
                    new_id = db.run(
                        "INSERT INTO files (client_id, rel_path, original_name, size, sha256, uploaded_by, uploaded_at, note,"
                        " folder_id, src_mtime) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
                        owner["id"], dest.relative_to(settings.clients_dir).as_posix(), path.name, st.st_size, fp,
                        admin["id"], now_iso(), fid, st.st_mtime)
                    thumbs.generate_async(dest, settings.thumbs_dir, new_id, fp)
                done += 1
                done_bytes += st.st_size
                if done % 25 == 0 or done == len(todo):
                    db.run("UPDATE import_jobs SET done_files = ?, done_bytes = ?, skipped = ?, current = ? WHERE id = ?",
                           done, done_bytes, skipped, f"{rel_dir}/{path.name}".strip("/")[:300], job_id)
            for name in names:  # sprzątnij puste katalogi po imporcie
                src = base / name
                if src.is_dir() and storage.inside(base, src):
                    for dp, _, _ in sorted(os.walk(src), key=lambda t: -len(t[0])):
                        try:
                            os.rmdir(dp)
                        except OSError:
                            pass
            db.run("UPDATE import_jobs SET status = 'done', done_files = ?, done_bytes = ?, skipped = ?, current = '',"
                   " finished_at = ? WHERE id = ?", done, done_bytes, skipped, now_iso(), job_id)
            db.log(admin["id"], "import_done", f"{done - skipped} plików, pominięto {skipped}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Import nie powiódł się")
            db.run("UPDATE import_jobs SET status = 'error', error = ?, finished_at = ? WHERE id = ?", str(exc)[:500], now_iso(), job_id)
        finally:
            import_lock.release()

    def import_status() -> dict | None:
        job = db.one("SELECT * FROM import_jobs ORDER BY id DESC LIMIT 1")
        return dict(job) if job else None

    @app.get("/api/import")
    def api_import(request: Request):
        require_user(request, "admin")
        return {"entries": import_entries(), "job": import_status(), "running": import_lock.locked(),
                "thumbs_pending": thumbs.pending()}

    @app.post("/admin/import")
    def start_import(request: Request, owner_id: int = Form(...), folder_id: int | None = Form(None),
                     names: list[str] = Form([]), csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        owner = space_owner_for_admin(admin, owner_id)
        folder_row(owner_id, folder_id)
        available = {e["name"] for e in import_entries()}
        chosen = [n for n in names if n in available]
        if not chosen:
            flash(request, "Zaznacz foldery lub pliki do importu.", "error")
        elif not import_lock.acquire(blocking=False):
            flash(request, "Trwa już inny import — poczekaj, aż się skończy.", "warn")
        else:
            job = db.run("INSERT INTO import_jobs (owner_id, folder_id, items, started_by, started_at) VALUES (?, ?, ?, ?, ?)",
                         owner_id, folder_id, json.dumps(chosen), admin["id"], now_iso())
            threading.Thread(target=run_import, args=(job, owner, folder_id, chosen, admin), daemon=True,
                             name="import").start()
            db.log(admin["id"], "import_started", ", ".join(chosen)[:500])
            flash(request, "Import ruszył — postęp widać poniżej. Możesz zamknąć stronę, import działa na serwerze.")
        return RedirectResponse(space_url(admin, owner_id, folder_id).replace("#pliki", "#import"), 303)

    @app.get("/api/connection")
    def api_connection(request: Request):
        """Którędy łączy się przeglądarka: sieć biurowa (prosto do NAS) czy internet (Cloudflare)."""
        via_cf = bool(request.headers.get("cf-ray") or request.headers.get("cf-connecting-ip"))
        return {"lan": not via_cf}

    # ------------------------------------------------------------------ admin: klienci

    @app.get("/admin", response_class=HTMLResponse)
    def admin_home(request: Request):
        require_user(request, "admin")
        since = (date.today() - timedelta(days=29)).isoformat()
        kpi = {
            "clients": db.one("SELECT COUNT(*) AS n FROM users WHERE role = 'client' AND space_id IS NULL AND active = 1")["n"],
            "client_files_30": db.one(
                "SELECT COUNT(*) AS n, COALESCE(SUM(f.size), 0) AS b FROM files f JOIN users u ON u.id = f.uploaded_by"
                " WHERE u.role = 'client' AND f.deleted_at IS NULL AND substr(f.uploaded_at, 1, 10) >= ?", since),
            "storage": db.one("SELECT COUNT(*) AS n, COALESCE(SUM(f.size), 0) AS b FROM files f JOIN users c"
                              " ON c.id = f.client_id WHERE c.role = 'client' AND f.deleted_at IS NULL"),
            "backups": db.one("SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM backup_files"),
            "links": db.one(
                "SELECT (SELECT COUNT(*) FROM share_links WHERE revoked = 0 AND (expires_at IS NULL OR expires_at > ?))"
                " + (SELECT COUNT(*) FROM transfers WHERE revoked = 0 AND files_deleted = 0 AND expires_at > ?) AS n,"
                " (SELECT COALESCE(SUM(downloads), 0) FROM share_links) + (SELECT COALESCE(SUM(downloads), 0) FROM transfers) AS d",
                now_iso(), now_iso()),
        }
        purge_old_trash()
        days = daily_files()
        clients = client_rows()
        top = sorted(clients, key=lambda c: c["total_size"], reverse=True)[:8]
        recent = db.all(
            "SELECT f.*, u.display_name, u.company, u.username, up.role AS uploader_role FROM files f"
            " JOIN users u ON u.id = f.client_id JOIN users up ON up.id = f.uploaded_by"
            " WHERE u.role = 'client' AND f.deleted_at IS NULL ORDER BY f.uploaded_at DESC LIMIT 8"
        )
        devices = device_rows()
        return render(
            request, "admin_home.html", kpi=kpi, days=days, top=top, recent=recent, devices=devices,
            disks=disks(), spark=[d["client"] for d in days][-14:],
            backup_status=backup.last_status(settings), backup_running=backup_lock.locked(),
            devices_online=sum(1 for d in devices if online(d["last_seen"])), nav="dashboard",
        )

    @app.get("/admin/clients", response_class=HTMLResponse)
    def admin_clients(request: Request, q: str = ""):
        require_user(request, "admin")
        clients = client_rows()
        if q.strip():
            ql = q.strip().lower()
            clients = [c for c in clients if ql in " ".join(
                [c["username"], c["display_name"], c["company"], c["email"]]).lower()]
        admin = require_user(request, "admin")
        return render(request, "admin_clients.html", clients=clients, q=q, nav="clients",
                      unread=unread_by_client(admin["id"]))

    @app.get("/admin/devices", response_class=HTMLResponse)
    def admin_devices(request: Request):
        require_user(request, "admin")
        return render(request, "admin_devices.html", devices=device_rows(), nav="devices")

    backup_lock = threading.Lock()

    @app.post("/admin/backup/run")
    def backup_now(request: Request, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        if not settings.backup_dir:
            raise HTTPException(400, "Kopia nie jest skonfigurowana (BSK_BACKUP_DIR).")
        if backup_lock.acquire(blocking=False):
            def job():
                try:
                    backup.run_backup(settings, db)
                except Exception as exc:  # noqa: BLE001
                    db.log(None, "backup_failed", str(exc)[:300])
                finally:
                    backup_lock.release()
            threading.Thread(target=job, daemon=True).start()
            db.log(admin["id"], "backup_started")
            flash(request, "Kopia uruchomiona — odśwież stronę za chwilę, aby zobaczyć wynik.")
        else:
            flash(request, "Kopia już trwa.", "warn")
        return RedirectResponse("/admin", 303)

    @app.get("/admin/clients/new", response_class=HTMLResponse)
    def new_client_form(request: Request):
        require_user(request, "admin")
        return render(
            request, "admin_client_new.html", suggested=security.generate_password(),
            default_quota=settings.default_quota_mb, nav="clients",
        )

    @app.post("/admin/clients/new")
    def create_client(
        request: Request,
        username: str = Form(...),
        display_name: str = Form(""),
        company: str = Form(""),
        email: str = Form(""),
        password: str = Form(...),
        quota_mb: int = Form(0),
        quota_gb: str | None = Form(None),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        quota_mb = parse_quota(quota_gb, quota_mb)
        username = username.strip()
        error = None
        if not USERNAME_RE.match(username):
            error = "Login: 3–40 znaków, litery bez polskich znaków, cyfry, kropka, myślnik, podkreślnik."
        elif db.one("SELECT 1 FROM users WHERE username = ?", username):
            error = "Taki login już istnieje."
        elif problem := security.password_problem(password):
            error = problem
        if error:
            flash(request, error, "error")
            return RedirectResponse("/admin/clients/new", 303)
        cid = db.run(
            "INSERT INTO users (username, password_hash, role, display_name, company, email,"
            " must_change_password, created_at, quota_mb) VALUES (?, ?, 'client', ?, ?, ?, 1, ?, ?)",
            username, security.hash_password(password), display_name.strip()[:120],
            company.strip()[:120], email.strip()[:200], now_iso(), max(0, quota_mb),
        )
        client_dir(db.one("SELECT * FROM users WHERE id = ?", cid)).mkdir(parents=True, exist_ok=True)
        db.log(admin["id"], "client_created", username)
        request.session["new_credentials"] = [username, password]
        return RedirectResponse(f"/admin/clients/{cid}", 303)

    @app.get("/admin/clients/{client_id}", response_class=HTMLResponse)
    def admin_client(request: Request, client_id: int, folder: int | None = None, show: int = 0):
        require_user(request, "admin")
        client = client_or_404(client_id)
        space = space_ctx(client, folder, show)
        creds = request.session.pop("new_credentials", None)
        mark_seen(require_user(request, "admin"), client_id)
        return render(
            request, "admin_client.html", client=client, files=client_files(client_id),
            messages=thread(client_id), credentials=creds, public_url=settings.public_url,
            quota=quota_info(client), days=daily_files(client_id), nav="clients", space=space,
            members=space_members(client_id), member_credentials=request.session.pop("member_credentials", None),
            base_url=f"/admin/clients/{client_id}", upload_action=f"/admin/clients/{client_id}/upload",
            folder=client_dir(client).as_posix().replace(settings.data_dir.as_posix(), "", 1),
        )

    @app.post("/admin/clients/{client_id}/edit")
    def edit_client(
        request: Request,
        client_id: int,
        display_name: str = Form(""),
        company: str = Form(""),
        email: str = Form(""),
        quota_mb: int = Form(0),
        quota_gb: str | None = Form(None),
        notify_email: str = Form(""),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        client_or_404(client_id)
        quota_mb = parse_quota(quota_gb, quota_mb)
        db.run(
            "UPDATE users SET display_name = ?, company = ?, email = ?, quota_mb = ?, notify_email = ? WHERE id = ?",
            display_name.strip()[:120], company.strip()[:120], email.strip()[:200], max(0, quota_mb),
            1 if notify_email else 0, client_id,
        )
        db.log(admin["id"], "client_edited", str(client_id))
        flash(request, "Zapisano dane klienta.")
        return RedirectResponse(f"/admin/clients/{client_id}", 303)

    @app.post("/admin/clients/{client_id}/reset-password")
    def reset_password(request: Request, client_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        client = client_or_404(client_id)
        password = security.generate_password()
        db.run(
            "UPDATE users SET password_hash = ?, must_change_password = 1,"
            " session_version = session_version + 1 WHERE id = ?",
            security.hash_password(password), client_id,
        )
        db.log(admin["id"], "password_reset", client["username"])
        request.session["new_credentials"] = [client["username"], password]
        return RedirectResponse(f"/admin/clients/{client_id}", 303)

    @app.post("/admin/clients/{client_id}/toggle")
    def toggle_client(request: Request, client_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        client = client_or_404(client_id)
        active = 0 if client["active"] else 1
        db.run(
            "UPDATE users SET active = ?, session_version = session_version + 1 WHERE id = ?",
            active, client_id,
        )
        db.log(admin["id"], "client_activated" if active else "client_blocked", client["username"])
        flash(request, "Konto odblokowane." if active else "Konto zablokowane — klient nie może się zalogować.")
        return RedirectResponse(f"/admin/clients/{client_id}", 303)

    @app.post("/admin/clients/{client_id}/upload")
    def admin_upload(
        request: Request,
        client_id: int,
        files: list[UploadFile] | None = File(None),
        upload_ids: list[str] = Form([]),
        note: str = Form(""),
        folder_id: int | None = Form(None),
        review: str = Form(""),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        client = client_or_404(client_id)
        count, names = 0, []
        for up in incoming(admin, files, upload_ids):
            fid = save_into(client, admin, up, note, folder_id)
            if not fid:
                continue
            if review:
                db.run("UPDATE files SET review_status = 'review', review_by = NULL, review_at = NULL WHERE id = ?", fid)
            names.append(up.filename)
            count += 1
        if review and names:
            add_message(client_id, admin, "🔎 Do akceptacji: " + ", ".join(f"„{n}”" for n in names))
        if note.strip():
            add_message(client_id, admin, note)
        if count:
            notify_client(client, "Nowe pliki od BSK Media",
                          [f"Udostępniliśmy Ci plików: {count}.", note.strip()])
        flash(request, f"Udostępniono klientowi plików: {count}.")
        return RedirectResponse(space_url(admin, client_id, folder_id), 303)

    @app.post("/admin/clients/{client_id}/message")
    def admin_message(request: Request, client_id: int, body: str = Form(...), csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        client = client_or_404(client_id)
        add_message(client_id, admin, body)
        mark_seen(admin, client_id)
        if body.strip():
            notify_client(client, "Nowa wiadomość od BSK Media", [body.strip()[:1500]])
        return RedirectResponse(f"/admin/clients/{client_id}#wiadomosci", 303)

    # ------------------------------------------------------------------ osoby z firmy klienta

    def member_or_404(member_id: int):
        m = db.one("SELECT * FROM users WHERE id = ? AND role = 'client' AND space_id IS NOT NULL", member_id)
        if not m:
            raise HTTPException(404, "Nie ma takiej osoby.")
        return m

    @app.post("/admin/clients/{client_id}/members")
    def add_member(request: Request, client_id: int, username: str = Form(...), display_name: str = Form(""),
                   email: str = Form(""), csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        client = client_or_404(client_id)
        username = username.strip()
        if not USERNAME_RE.match(username):
            flash(request, "Login: 3–40 znaków, litery bez polskich znaków, cyfry, kropka, myślnik (bez _ na początku).", "error")
        elif db.one("SELECT 1 FROM users WHERE username = ?", username):
            flash(request, "Taki login już istnieje.", "error")
        else:
            password = security.generate_password()
            db.run("INSERT INTO users (username, password_hash, role, display_name, company, email, must_change_password,"
                   " created_at, space_id) VALUES (?, ?, 'client', ?, ?, ?, 1, ?, ?)",
                   username, security.hash_password(password), display_name.strip()[:120], client["company"],
                   email.strip()[:200], now_iso(), client_id)
            db.log(admin["id"], "member_created", f"{username} -> {client['username']}")
            request.session["member_credentials"] = [username, password]
        return RedirectResponse(f"/admin/clients/{client_id}#osoby", 303)

    @app.post("/admin/members/{member_id}/reset-password")
    def member_reset(request: Request, member_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        m = member_or_404(member_id)
        password = security.generate_password()
        db.run("UPDATE users SET password_hash = ?, must_change_password = 1, session_version = session_version + 1"
               " WHERE id = ?", security.hash_password(password), member_id)
        db.log(admin["id"], "password_reset", m["username"])
        request.session["member_credentials"] = [m["username"], password]
        return RedirectResponse(f"/admin/clients/{m['space_id']}#osoby", 303)

    @app.post("/admin/members/{member_id}/toggle")
    def member_toggle(request: Request, member_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        m = member_or_404(member_id)
        db.run("UPDATE users SET active = ?, session_version = session_version + 1 WHERE id = ?",
               0 if m["active"] else 1, member_id)
        db.log(admin["id"], "member_blocked" if m["active"] else "member_activated", m["username"])
        flash(request, f"Konto {m['username']} {'zablokowane' if m['active'] else 'odblokowane'}.")
        return RedirectResponse(f"/admin/clients/{m['space_id']}#osoby", 303)

    @app.post("/admin/members/{member_id}/delete")
    def member_delete(request: Request, member_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        m = member_or_404(member_id)
        # pliki i wiadomości zostają w przestrzeni firmy; konto tylko się wyłącza i znika z listy
        # space_id = -1: konto archiwalne (nie loguje się, nie widać go na listach, wiadomości zachowują autora)
        db.run("UPDATE users SET active = 0, space_id = -1, username = ?, session_version = session_version + 1"
               " WHERE id = ?", f"usuniety-{member_id}-{secrets.token_hex(3)}", member_id)
        db.log(admin["id"], "member_deleted", m["username"])
        flash(request, f"Usunięto dostęp dla {m['display_name'] or m['username']}.")
        return RedirectResponse(f"/admin/clients/{m['space_id']}#osoby", 303)

    # ------------------------------------------------------------------ admin: urządzenia (kopie)

    @app.get("/admin/devices/new", response_class=HTMLResponse)
    def new_device_form(request: Request):
        require_user(request, "admin")
        return render(request, "admin_device_new.html", nav="devices", special=SPECIAL_FOLDERS)

    @app.post("/admin/devices/new")
    def create_device(
        request: Request,
        name: str = Form(...),
        employee: str = Form(""),
        quota_mb: int = Form(0),
        quota_gb: str | None = Form(None),
        special: list[str] = Form([]),
        extra_folders: str = Form(""),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        quota_mb = parse_quota(quota_gb, quota_mb)
        folders = folders_text(special, extra_folders)
        name = name.strip()[:80]
        if not name or db.one("SELECT 1 FROM devices WHERE name = ?", name):
            flash(request, "Podaj unikalną nazwę komputera.", "error")
            return RedirectResponse("/admin/devices/new", 303)
        folder = storage.safe_slug(name)
        base_folder, i = folder, 2
        while db.one("SELECT 1 FROM devices WHERE folder_name = ?", folder):
            folder, i = f"{base_folder}-{i}", i + 1
        token = security.new_device_token()
        did = db.run(
            "INSERT INTO devices (name, employee, folder_name, token_hash, created_at, quota_mb, folders)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            name, employee.strip()[:120], folder, security.token_hash(token), now_iso(), quota_mb, folders,
        )
        (settings.backups_dir / folder).mkdir(parents=True, exist_ok=True)
        db.log(admin["id"], "device_created", name)
        create_install_link(did, token)
        request.session["new_token"] = token
        return RedirectResponse(f"/admin/devices/{did}", 303)

    def folders_text(special: list[str], extra: str) -> str:
        """Foldery do kopiowania: specjalne (@pulpit…) + własne ścieżki, po jednej w linii."""
        chosen = [f for f in special if f in SPECIAL_FOLDERS]
        paths = [ln.strip() for ln in extra.splitlines() if ln.strip() and not ln.strip().startswith("@")]
        return "\n".join(chosen + paths[:30])

    def device_or_404(device_id: int):
        d = db.one("SELECT * FROM devices WHERE id = ?", device_id)
        if not d:
            raise HTTPException(404, "Nie ma takiego urządzenia.")
        return d

    def backup_dir_listing(device_id: int, folder: str) -> tuple[list[dict], list]:
        """Podkatalogi (z liczbą plików, rozmiarem, datą) i pliki bezpośrednio w katalogu kopii."""
        prefix = f"{folder}/" if folder else ""
        n = len(prefix)
        rest = "substr(rel_path, ? + 1)"
        dirs = db.all(
            f"SELECT substr({rest}, 1, instr({rest}, '/') - 1) AS name, COUNT(*) AS n, COALESCE(SUM(size), 0) AS size,"
            " MAX(uploaded_at) AS uploaded_at FROM backup_files"
            f" WHERE device_id = ? AND substr(rel_path, 1, ?) = ? AND instr({rest}, '/') > 0"
            " GROUP BY name ORDER BY lower(name)",
            n, n, device_id, n, prefix, n)
        files = db.all(
            f"SELECT * FROM backup_files WHERE device_id = ? AND substr(rel_path, 1, ?) = ? AND instr({rest}, '/') = 0"
            " ORDER BY lower(rel_path)", device_id, n, prefix, n)
        return [dict(d) for d in dirs], files

    def clean_backup_dir(folder: str) -> str:
        try:
            return storage.safe_rel_path(folder) if folder.strip("/ ") else ""
        except ValueError:
            raise HTTPException(400, "Nieprawidłowa ścieżka.")

    @app.get("/admin/devices/{device_id}/zip")
    def backup_folder_zip(request: Request, device_id: int, dir: str = ""):
        """Cały katalog z kopii komputera jako ZIP (generowany w locie, ze strukturą podkatalogów)."""
        admin = require_user(request, "admin")
        device = device_or_404(device_id)
        folder = clean_backup_dir(dir)
        prefix = f"{folder}/" if folder else ""
        base = settings.backups_dir / device["folder_name"]
        rows = db.all("SELECT rel_path FROM backup_files WHERE device_id = ? AND substr(rel_path, 1, ?) = ?"
                      " ORDER BY rel_path", device_id, len(prefix), prefix)
        entries = [(r["rel_path"][len(prefix):], base / r["rel_path"]) for r in rows
                   if (base / r["rel_path"]).is_file() and storage.inside(base, base / r["rel_path"])]
        if not entries:
            raise HTTPException(404, "Ten katalog jest pusty.")
        db.log(admin["id"], "backup_zip", f"{device['name']}: /{folder} ({len(entries)} plików)")
        name = storage.safe_name(f"{device['name']} - {folder.rsplit('/', 1)[-1] if folder else 'kopia'}")[:100] + ".zip"
        return StreamingResponse(transfers.zip_stream(entries), media_type="application/zip", headers={
            "Content-Disposition": f"attachment; filename=\"kopia.zip\"; filename*=UTF-8''{quote(name)}",
            "Cache-Control": "no-store"})

    @app.get("/admin/devices/{device_id}", response_class=HTMLResponse)
    def admin_device(request: Request, device_id: int, q: str = "", page: int = 1, dir: str = ""):
        require_user(request, "admin")
        device = device_or_404(device_id)
        folder = clean_backup_dir(dir)
        page = max(1, page)
        per_page = 200
        like = f"%{q.strip()}%"
        if q.strip():
            rows = db.all(
                "SELECT * FROM backup_files WHERE device_id = ? AND rel_path LIKE ?"
                " ORDER BY uploaded_at DESC LIMIT ? OFFSET ?",
                device_id, like, per_page + 1, (page - 1) * per_page,
            )
            subdirs = []
        else:
            subdirs, rows = backup_dir_listing(device_id, folder)
            rows = rows[(page - 1) * per_page:page * per_page + 1]
        stats = db.one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS total FROM backup_files WHERE device_id = ?",
            device_id,
        )
        link = db.one(
            "SELECT * FROM install_links WHERE device_id = ? AND expires_at > ? ORDER BY id DESC LIMIT 1",
            device_id, now_iso(),
        )
        return render(
            request, "admin_device.html", device=device, files=rows[:per_page], has_more=len(rows) > per_page,
            page=page, q=q, stats=stats, folder=folder, subdirs=subdirs,
            crumbs=[("/".join(folder.split("/")[:i + 1]), part) for i, part in enumerate(folder.split("/"))] if folder else [], token=request.session.pop("new_token", None),
            public_url=settings.public_url, install_link=link, installer_ready=installer_ready(),
            days=daily_backups(device_id), nav="devices", special=SPECIAL_FOLDERS,
            report=agents.parse_report(device["agent_report"]), latest_version=agents.installer_version(settings),
            newer=agents.newer,
            chosen=[ln for ln in device["folders"].splitlines() if ln.startswith("@")],
            extra="\n".join(ln for ln in device["folders"].splitlines() if ln and not ln.startswith("@")),
        )

    @app.post("/admin/devices/{device_id}/settings")
    def device_settings(
        request: Request,
        device_id: int,
        employee: str = Form(""),
        quota_mb: int = Form(0),
        quota_gb: str | None = Form(None),
        special: list[str] = Form([]),
        extra_folders: str = Form(""),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        device_or_404(device_id)
        db.run("UPDATE devices SET employee = ?, quota_mb = ?, folders = ? WHERE id = ?", employee.strip()[:120],
               parse_quota(quota_gb, quota_mb), folders_text(special, extra_folders), device_id)
        db.log(admin["id"], "device_edited", str(device_id))
        flash(request, "Zapisano ustawienia komputera.")
        return RedirectResponse(f"/admin/devices/{device_id}", 303)

    @app.post("/admin/devices/{device_id}/install-link")
    def new_install_link(request: Request, device_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        device = device_or_404(device_id)
        token = security.new_device_token()
        db.run("UPDATE devices SET token_hash = ?, active = 1 WHERE id = ?", security.token_hash(token), device_id)
        create_install_link(device_id, token)
        db.log(admin["id"], "install_link_created", device["name"])
        flash(request, "Utworzono nowy link instalacyjny. Poprzedni token tego komputera przestał działać.")
        return RedirectResponse(f"/admin/devices/{device_id}", 303)

    @app.post("/admin/devices/{device_id}/token")
    def rotate_token(request: Request, device_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        device = device_or_404(device_id)
        token = security.new_device_token()
        db.run("UPDATE devices SET token_hash = ?, active = 1 WHERE id = ?", security.token_hash(token), device_id)
        db.run("DELETE FROM install_links WHERE device_id = ?", device_id)
        db.log(admin["id"], "device_token_rotated", device["name"])
        request.session["new_token"] = token
        return RedirectResponse(f"/admin/devices/{device_id}", 303)

    @app.post("/admin/devices/{device_id}/sync")
    def request_sync(request: Request, device_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        device = device_or_404(device_id)
        db.run("UPDATE devices SET sync_requested = ? WHERE id = ?", now_iso(), device_id)
        db.log(admin["id"], "device_sync_requested", device["name"])
        flash(request, "Zlecono synchronizację — program wykona ją w ciągu ~2 minut, gdy komputer jest włączony"
              " (starsze wersje programu: po aktualizacji).")
        return RedirectResponse(f"/admin/devices/{device_id}", 303)

    @app.post("/admin/devices/{device_id}/toggle")
    def toggle_device(request: Request, device_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        device = device_or_404(device_id)
        active = 0 if device["active"] else 1
        db.run("UPDATE devices SET active = ? WHERE id = ?", active, device_id)
        db.log(admin["id"], "device_enabled" if active else "device_disabled", device["name"])
        flash(request, "Urządzenie włączone." if active else "Urządzenie wyłączone — agent nie może wysyłać kopii.")
        return RedirectResponse(f"/admin/devices/{device_id}", 303)

    def backup_path(device, rel: str) -> tuple[Path, Path]:
        base = settings.backups_dir / device["folder_name"]
        rel = storage.safe_rel_path(rel)
        target = base / rel
        if not storage.inside(base, target):
            raise HTTPException(400, "Nieprawidłowa ścieżka.")
        return base, target

    @app.get("/admin/devices/{device_id}/download")
    def backup_download(request: Request, device_id: int, path: str, version: str = ""):
        require_user(request, "admin")
        device = device_or_404(device_id)
        base, target = backup_path(device, path)
        name = target.name
        if version:
            rel = target.relative_to(base).as_posix()
            vdir = (base / ".wersje" / rel).parent
            candidate = vdir / storage.safe_name(version)
            if not candidate.name.startswith(target.name + ".") or not storage.inside(base, candidate):
                raise HTTPException(404, "Nie ma takiej wersji.")
            stamp = candidate.name[len(target.name) + 1:]
            name = f"{target.stem} (wersja {stamp}){target.suffix}"
            target = candidate
        if not target.is_file():
            raise HTTPException(404, "Plik nie istnieje.")
        return FileResponse(target, filename=name)

    @app.get("/admin/devices/{device_id}/versions", response_class=HTMLResponse)
    def backup_versions(request: Request, device_id: int, path: str):
        require_user(request, "admin")
        device = device_or_404(device_id)
        base, target = backup_path(device, path)
        rel = target.relative_to(base).as_posix()
        vdir = (base / ".wersje" / rel).parent
        versions = []
        if vdir.is_dir():
            for p in sorted(vdir.iterdir(), key=lambda p: p.name, reverse=True):
                if p.is_file() and p.name.startswith(target.name + "."):
                    versions.append({"name": p.name, "stamp": p.name[len(target.name) + 1:], "size": p.stat().st_size})
        current = db.one("SELECT * FROM backup_files WHERE device_id = ? AND rel_path = ?", device_id, rel)
        return render(request, "admin_versions.html", device=device, rel=rel, versions=versions, current=current,
                      nav="devices")

    @app.get("/admin/log", response_class=HTMLResponse)
    def admin_log(request: Request):
        require_user(request, "admin")
        events = db.all(
            "SELECT e.*, u.username FROM events e LEFT JOIN users u ON u.id = e.user_id ORDER BY e.id DESC LIMIT 300"
        )
        return render(request, "admin_log.html", events=events, nav="log")

    # ------------------------------------------------------------------ instalator agenta (link dla pracownika)

    INSTALLER_FILES = ("bsk-backup.exe", "bsk-backup-agent.exe", "install-windows.ps1", "uninstall-windows.ps1")

    def installer_ready() -> bool:
        return all((settings.installer_dir / f).is_file() for f in INSTALLER_FILES)

    def create_install_link(device_id: int, token: str) -> str:
        code = security.new_link_token()
        db.run("DELETE FROM install_links WHERE device_id = ?", device_id)
        db.run(
            "INSERT INTO install_links (code, device_id, token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            code, device_id, token, now_iso(), iso_in(7),
        )
        return code

    def install_link_or_404(code: str):
        db.run("DELETE FROM install_links WHERE expires_at <= ?", now_iso())  # tokeny nie leżą dłużej niż trzeba
        link = db.one(
            "SELECT l.*, d.name AS device_name, d.employee, d.active FROM install_links l"
            " JOIN devices d ON d.id = l.device_id WHERE l.code = ?",
            code,
        )
        if not link or not link["active"] or link["downloads"] >= 5:
            raise HTTPException(404, "Link instalacyjny wygasł lub jest nieprawidłowy. Poproś administratora o nowy.")
        return link

    @app.get("/instaluj/{code}", response_class=HTMLResponse)
    def install_page(request: Request, code: str):
        link = install_link_or_404(code)
        return render(request, "install.html", user=None, link=link, ready=installer_ready(), code=code)

    @app.get("/instaluj/{code}/BSK-Backup.zip")
    def install_download(request: Request, code: str):
        link = install_link_or_404(code)
        if not installer_ready():
            raise HTTPException(503, "Instalator nie został jeszcze wgrany na serwer. Daj znać administratorowi.")
        fd, tmp_name = tempfile.mkstemp(dir=settings.tmp_dir, prefix="installer-", suffix=".zip")
        os.close(fd)
        launcher = (
            "@echo off\r\n"
            "rem Instalacja BSK Backup dla komputera: " + link["device_name"].replace("%", "") + "\r\n"
            'cd /d "%~dp0"\r\n'
            'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1" '
            f'-Server "{settings.public_url}" -Token "{link["token"]}"'
            + (f' -LanServer "{settings.lan_url}"' if settings.lan_url else "") + "\r\n"
            "echo.\r\npause\r\n"
        )
        readme = (
            "BSK Backup - kopia zapasowa plików na serwer BSK Media\r\n\r\n"
            "1. Rozpakuj ten plik ZIP (prawy przycisk -> Wyodrębnij wszystkie).\r\n"
            "2. Kliknij dwukrotnie ZAINSTALUJ.cmd.\r\n"
            "3. Jeśli Windows pokaże ostrzeżenie SmartScreen: 'Więcej informacji' -> 'Uruchom mimo to'.\r\n\r\n"
            "Program działa w tle i uruchamia się sam po zalogowaniu.\r\n"
        )
        with zipfile.ZipFile(tmp_name, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for f in INSTALLER_FILES:
                if f.endswith(".ps1"):
                    # Windows PowerShell 5.1 czyta skrypt bez BOM jako ANSI — polskie znaki psują wtedy składnię
                    data = (settings.installer_dir / f).read_bytes()
                    z.writestr(f"BSK-Backup/{f}", data if data.startswith(b"\xef\xbb\xbf") else b"\xef\xbb\xbf" + data)
                else:
                    z.write(settings.installer_dir / f, f"BSK-Backup/{f}")
            z.writestr("BSK-Backup/ZAINSTALUJ.cmd", launcher)
            z.writestr("BSK-Backup/CZYTAJ.txt", readme)
        db.run("UPDATE install_links SET downloads = downloads + 1 WHERE id = ?", link["id"])
        db.log(None, "installer_downloaded", f"{link['device_name']} z {client_ip(request)}")
        return FileResponse(
            tmp_name, filename="BSK-Backup.zip", media_type="application/zip",
            background=BackgroundTask(os.unlink, tmp_name),
        )

    # ------------------------------------------------------------------ linki do pobierania (udostępnianie)

    def shared_files() -> list[dict]:
        out = []
        base = settings.shared_dir
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "@", "#")))
            for name in sorted(filenames):
                if name.startswith((".", "@")):
                    continue
                p = Path(dirpath) / name
                try:
                    out.append({"rel": p.relative_to(base).as_posix(), "size": p.stat().st_size})
                except OSError:
                    continue
                if len(out) >= 2000:
                    return out
        return out

    def link_url(token: str) -> str:
        return f"{settings.public_url}/s/{token}"

    def create_share(admin, *, kind: str, name: str, size: int, file_id=None, rel_path: str = "",
                     expires_days: int = 7, max_downloads: int = 0, password: str = "", note: str = "") -> str:
        token = security.new_link_token()
        db.run(
            "INSERT INTO share_links (token, kind, file_id, rel_path, name, size, note, password_hash, expires_at,"
            " max_downloads, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            token, kind, file_id, rel_path, name, size, note.strip()[:300],
            security.hash_password(password) if password else "",
            iso_in(expires_days) if expires_days > 0 else None, max(0, max_downloads), admin["id"], now_iso(),
        )
        db.log(admin["id"], "share_created", f"{name} -> /s/{token[:6]}…")
        return link_url(token)

    @app.get("/admin/shares", response_class=HTMLResponse)
    def shares_page(request: Request):
        require_user(request, "admin")
        links = db.all(
            "SELECT s.*, f.client_id FROM share_links s LEFT JOIN files f ON f.id = s.file_id"
            " ORDER BY s.revoked, s.id DESC LIMIT 300"
        )
        return render(
            request, "admin_shares.html", links=links, nav="shares", shared=shared_files(), now=now_iso(),
            public_url=settings.public_url, shared_folder="/udostepnione",
        )

    def share_options(expires_days: int, max_downloads: int, password: str) -> dict:
        return {"expires_days": max(0, min(expires_days, 3650)), "max_downloads": max(0, max_downloads),
                "password": password.strip()}

    @app.post("/admin/shares/upload")
    def share_upload(
        request: Request,
        files: list[UploadFile] | None = File(None),
        upload_ids: list[str] = Form([]),
        expires_days: int = Form(7),
        max_downloads: int = Form(0),
        password: str = Form(""),
        note: str = Form(""),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        made = []
        for up in incoming(admin, files, upload_ids):
            dest = storage.unique_path(settings.shared_dir, storage.safe_name(up.filename))
            try:
                size, _ = place(up, dest, max_upload)
            except storage.TooLarge:
                raise HTTPException(413, f"Plik przekracza limit {settings.max_upload_mb} MB.")
            rel = dest.relative_to(settings.shared_dir).as_posix()
            made.append(create_share(admin, kind="shared", name=dest.name, size=size, rel_path=rel, note=note,
                                     **share_options(expires_days, max_downloads, password)))
        request.session["new_link"] = made
        return RedirectResponse("/admin/shares", 303)

    @app.post("/admin/shares/new")
    def share_new(
        request: Request,
        kind: str = Form(...),
        rel_path: str = Form(""),
        file_id: int = Form(0),
        expires_days: int = Form(7),
        max_downloads: int = Form(0),
        password: str = Form(""),
        note: str = Form(""),
        back: str = Form("/admin/shares"),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        opts = share_options(expires_days, max_downloads, password)
        if kind == "file":
            f, path = file_for(admin, file_id)
            url = create_share(admin, kind="file", name=f["original_name"], size=f["size"], file_id=f["id"],
                               note=note, **opts)
        elif kind == "shared":
            try:
                rel = storage.safe_rel_path(rel_path)
            except ValueError:
                raise HTTPException(400, "Nieprawidłowa ścieżka.")
            path = settings.shared_dir / rel
            if rel != rel_path or not storage.inside(settings.shared_dir, path) or not path.is_file():
                raise HTTPException(404, "Nie ma takiego pliku w folderze udostepnione.")
            url = create_share(admin, kind="shared", name=path.name, size=path.stat().st_size, rel_path=rel,
                               note=note, **opts)
        else:
            raise HTTPException(400, "Nieznany typ.")
        request.session["new_link"] = [url]
        return RedirectResponse(back if back.startswith("/admin/") else "/admin/shares", 303)

    @app.post("/admin/shares/{share_id}/revoke")
    def share_revoke(request: Request, share_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        db.run("UPDATE share_links SET revoked = 1 WHERE id = ?", share_id)
        db.log(admin["id"], "share_revoked", str(share_id))
        flash(request, "Link wyłączony — nie da się już nim pobrać pliku.")
        return RedirectResponse("/admin/shares", 303)

    def share_or_404(token: str):
        link = db.one("SELECT * FROM share_links WHERE token = ?", token)
        problem = None
        if not link or link["revoked"]:
            problem = "Ten link jest nieprawidłowy albo został wyłączony."
        elif link["expires_at"] and link["expires_at"] <= now_iso():
            problem = "Ten link wygasł. Poproś BSK Media o nowy."
        elif link["max_downloads"] and link["downloads"] >= link["max_downloads"]:
            problem = "Wykorzystano limit pobrań dla tego linku."
        if problem:
            raise HTTPException(404, problem)
        if link["kind"] == "file":
            f = db.one("SELECT rel_path FROM files WHERE id = ? AND deleted_at IS NULL", link["file_id"])
            path = settings.clients_dir / f["rel_path"] if f else None
            base = settings.clients_dir
        else:
            path, base = settings.shared_dir / link["rel_path"], settings.shared_dir
        if not path or not storage.inside(base, path) or not path.is_file():
            raise HTTPException(404, "Plik nie jest już dostępny.")
        return link, path

    def share_unlocked(request: Request, link) -> bool:
        return not link["password_hash"] or link["id"] in request.session.get("shares_ok", [])

    @app.get("/s/{token}", response_class=HTMLResponse)
    def share_page(request: Request, token: str):
        link, path = share_or_404(token)
        return render(request, "share.html", user=None, link=link, token=token,
                      unlocked=share_unlocked(request, link))

    @app.post("/s/{token}")
    def share_unlock(request: Request, token: str, password: str = Form(""), csrf: str = Form("")):
        check_csrf(request, csrf)
        link, _ = share_or_404(token)
        ip = client_ip(request)
        if limiter.blocked("share:" + token, ip):
            flash(request, "Zbyt wiele prób. Spróbuj za 15 minut.", "error")
        elif security.verify_password(password, link["password_hash"]):
            ok = request.session.get("shares_ok", [])
            request.session["shares_ok"] = (ok + [link["id"]])[-20:]
        else:
            limiter.fail("share:" + token, ip)
            flash(request, "Nieprawidłowe hasło.", "error")
        return RedirectResponse(f"/s/{token}", 303)

    @app.get("/s/{token}/pobierz")
    def share_download(request: Request, token: str):
        link, path = share_or_404(token)
        if not share_unlocked(request, link):
            return RedirectResponse(f"/s/{token}", 303)
        rng = request.headers.get("range", "")
        if not rng or rng.replace(" ", "").startswith("bytes=0-"):
            # liczymy tylko rozpoczęcie pobierania, nie każdy wznowiony fragment
            db.run("UPDATE share_links SET downloads = downloads + 1, last_download = ? WHERE id = ?",
                   now_iso(), link["id"])
            db.log(None, "share_download", f"{link['name']} z {client_ip(request)}")
        return FileResponse(path, filename=link["name"])

    # ------------------------------------------------------------------ edycja online (ONLYOFFICE Docs)

    def office_ready() -> bool:
        return bool(settings.office_url and settings.office_secret)

    @app.get("/files/{file_id}/edit", response_class=HTMLResponse)
    def edit_online(request: Request, file_id: int, mode: str = "edit"):
        user = require_user(request)
        f, _ = file_for(user, file_id)
        if not office_ready():
            raise HTTPException(404, "Edycja online nie jest włączona na serwerze.")
        if not office.doc_type(f["original_name"]):
            raise HTTPException(400, "Tego typu pliku nie da się otworzyć w edytorze online.")
        cfg = office.editor_config(secret=settings.office_secret, file_row=f, user=user,
                                   internal_app_url=settings.app_internal_url, mode=mode)
        back = f"/admin/clients/{f['client_id']}" if user["role"] == "admin" else "/files"
        response = render(request, "editor.html", file=f, config=json.dumps(cfg), back=back,
                          office_url=settings.office_url, nav="clients" if user["role"] == "admin" else "files")
        # źródło CSP ze ścieżką musi kończyć się „/”, żeby obejmowało też pliki pod nią (/office/...)
        o = settings.office_url + ("/" if urllib.parse.urlsplit(settings.office_url).path else "")
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' {o}; frame-src {o}; connect-src 'self' {o}; "
            f"img-src 'self' data: {o}; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
        )
        db.log(user["id"], "office_open", f["rel_path"])
        return response

    def office_file_or_404(file_id: int, purpose: str, sig: str):
        if not office_ready() or not office.check_url(settings.office_secret, file_id, purpose, sig):
            raise HTTPException(403, "Brak dostępu.")
        f = db.one("SELECT * FROM files WHERE id = ? AND deleted_at IS NULL", file_id)
        if not f:
            raise HTTPException(404, "Nie ma takiego pliku.")
        path = settings.clients_dir / f["rel_path"]
        if not storage.inside(settings.clients_dir, path):
            raise HTTPException(404, "Nie ma takiego pliku.")
        return f, path

    @app.get("/office/file/{file_id}")
    def office_file(file_id: int, sig: str = ""):
        """Pobranie pliku przez serwer dokumentów (sieć wewnętrzna Dockera, adres podpisany)."""
        f, path = office_file_or_404(file_id, "file", sig)
        if not path.is_file():
            raise HTTPException(404, "Nie ma takiego pliku.")
        return FileResponse(path, filename=f["original_name"])

    @app.post("/office/callback/{file_id}")
    async def office_callback(request: Request, file_id: int, sig: str = ""):
        """Serwer dokumentów zgłasza stan edycji; przy statusie 2/6 zapisujemy nową wersję pliku."""
        f, path = office_file_or_404(file_id, "callback", sig)
        try:
            body = await request.json()
            token = body.get("token") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            data = office.jwt_decode(token, settings.office_secret)
            data = data.get("payload", data)
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": 1})
        status = int(data.get("status", 0))
        if status not in (2, 6):
            return {"error": 0}
        url = str(data.get("url", ""))
        if settings.office_url and url.startswith(settings.office_url):
            url = settings.office_internal_url + url[len(settings.office_url):]  # pobierz wewnątrz NAS
        if not url.startswith(("http://", "https://")):
            return JSONResponse({"error": 1})
        users = data.get("users") or []
        try:
            await asyncio.to_thread(save_office_version, f, path, url, users, status)
        except Exception:  # noqa: BLE001
            log.exception("Nie udało się zapisać pliku z edytora")
            return JSONResponse({"error": 1})
        return {"error": 0}

    def save_office_version(f, path: Path, url: str, users: list, status: int) -> None:
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=settings.tmp_dir, prefix="office-")
        digest, size = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310
                while chunk := resp.read(1024 * 1024):
                    digest.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
            if path.is_file():
                vdir = path.parent / ".wersje"
                vdir.mkdir(exist_ok=True)
                shutil.copy2(path, vdir / f"{path.name}.{storage.version_stamp()}")
                storage.prune_versions(vdir, path.name, 10)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        editor = None
        if users:
            try:
                editor = int(users[0])
            except (TypeError, ValueError):
                editor = None
        db.run(
            "UPDATE files SET size = ?, sha256 = ?, modified_at = ?, modified_by = ?,"
            " office_version = office_version + ? WHERE id = ?",
            size, digest.hexdigest(), now_iso(), editor, 1 if status == 2 else 0, f["id"],
        )
        db.log(editor, "office_saved", f"{f['rel_path']} ({size} B)")

    def create_blank(client, author, kind: str, title: str) -> int:
        content, ext = office.blank_document(kind)
        base = storage.safe_name((title.strip() or ("Nowy arkusz" if kind == "xlsx" else "Nowy dokument")))
        if not base.lower().endswith("." + ext):
            base += "." + ext
        target_dir = client_dir(client) / ("od BSK Media" if author["role"] == "admin" else "")
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = storage.unique_path(target_dir, base)
        dest.write_bytes(content)
        fid = db.run(
            "INSERT INTO files (client_id, rel_path, original_name, size, sha256, uploaded_by, uploaded_at, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, '')",
            client["id"], dest.relative_to(settings.clients_dir).as_posix(), dest.name, len(content),
            hashlib.sha256(content).hexdigest(), author["id"], now_iso(),
        )
        db.log(author["id"], "office_new", dest.name)
        return fid

    @app.post("/admin/clients/{client_id}/new-doc")
    def admin_new_doc(request: Request, client_id: int, kind: str = Form(...), title: str = Form(""),
                      csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        if kind not in ("xlsx", "docx"):
            raise HTTPException(400, "Nieznany typ dokumentu.")
        fid = create_blank(client_or_404(client_id), admin, kind, title)
        return RedirectResponse(f"/files/{fid}/edit" if office_ready() else f"/admin/clients/{client_id}", 303)

    @app.post("/files/new-doc")
    def client_new_doc(request: Request, kind: str = Form(...), title: str = Form(""), csrf: str = Form("")):
        check_csrf(request, csrf)
        user = require_user(request, "client")
        if kind not in ("xlsx", "docx"):
            raise HTTPException(400, "Nieznany typ dokumentu.")
        fid = create_blank(space_of(user), user, kind, title)
        return RedirectResponse(f"/files/{fid}/edit" if office_ready() else "/files", 303)

    # ------------------------------------------------------------------ akceptacja projektów i komentarze na plikach

    REVIEW_LABELS = {"": "", "review": "Do akceptacji", "approved": "Zaakceptowane", "changes": "Poprawki"}
    templates.env.globals["review_labels"] = REVIEW_LABELS

    def review_json(f, user) -> dict:
        rows = db.all(
            "SELECT a.*, u.display_name, u.username, u.role FROM annotations a JOIN users u ON u.id = a.author_id"
            " WHERE a.file_id = ? ORDER BY a.id", f["id"])
        by = db.one("SELECT display_name, username FROM users WHERE id = ?", f["review_by"]) if f["review_by"] else None
        pins = [r for r in rows if r["x"] is not None and r["version"] == f["version"]]
        numbers = {r["id"]: i + 1 for i, r in enumerate(pins)}
        return {
            "status": f["review_status"], "label": REVIEW_LABELS.get(f["review_status"], ""),
            "by": (by["display_name"] or by["username"]) if by else "", "at": _local_dt(f["review_at"]),
            "is_admin": user["role"] == "admin", "version": f["version"],
            "preview": f"/files/{f['id']}/thumb?s=preview&v={f['sha256'][:12]}" if thumbs.can_thumb(f["original_name"]) else "",
            "comments": [{
                "id": r["id"], "n": numbers.get(r["id"]), "x": r["x"], "y": r["y"], "body": r["body"],
                "author": r["display_name"] or r["username"], "initials": _initials(r["display_name"] or r["username"]),
                "agency": r["role"] == "admin", "time": _local_dt(r["created_at"]), "resolved": bool(r["resolved"]),
                "old": r["version"] != f["version"], "version": r["version"],
                "can_resolve": user["role"] == "admin" or r["author_id"] == user["id"],
            } for r in rows],
        }

    def review_file(request: Request, file_id: int):
        user = api_user(request)
        f, _ = file_for(user, file_id)
        return user, f

    def review_notify(f, user, text: str) -> None:
        """Ślad w rozmowie z klientem + e-mail do drugiej strony."""
        add_message(f["client_id"], user, text)
        owner = db.one("SELECT * FROM users WHERE id = ?", f["client_id"])
        if owner["role"] != "client":
            return
        if user["role"] == "admin":
            notify_client(owner, "Projekt czeka na Ciebie", [text])
        else:
            notify_admin(settings, f"{owner['company'] or owner['display_name'] or owner['username']}: {REVIEW_LABELS.get(f['review_status'], 'komentarz')}",
                         text, link=f"{settings.public_url}/admin/clients/{owner['id']}", throttle_key=f"admin:{owner['id']}")

    @app.get("/api/files/{file_id}/review")
    def api_review(request: Request, file_id: int):
        user, f = review_file(request, file_id)
        return review_json(f, user)

    @app.post("/api/files/{file_id}/comments")
    async def api_comment(request: Request, file_id: int):
        user, f = review_file(request, file_id)
        try:
            body = await request.json()
            text = str(body.get("body", "")).strip()[:2000]
            x, y = body.get("x"), body.get("y")
            if x is not None or y is not None:
                x, y = float(x), float(y)
                if not (0 <= x <= 1 and 0 <= y <= 1):
                    raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(400, "Nieprawidłowe dane.")
        if not text:
            raise HTTPException(400, "Wpisz treść komentarza.")
        db.run("INSERT INTO annotations (file_id, version, author_id, x, y, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
               f["id"], f["version"], user["id"], x, y, text, now_iso())
        name = user["display_name"] or user["username"]
        review_notify(f, user, f"📌 {name} — komentarz do „{f['original_name']}”: {text}")
        return review_json(db.one("SELECT * FROM files WHERE id = ?", f["id"]), user)

    @app.post("/api/files/{file_id}/comments/{comment_id}/resolve")
    def api_resolve(request: Request, file_id: int, comment_id: int):
        user, f = review_file(request, file_id)
        a = db.one("SELECT * FROM annotations WHERE id = ? AND file_id = ?", comment_id, f["id"])
        if not a or (user["role"] != "admin" and a["author_id"] != user["id"]):
            raise HTTPException(404, "Nie ma takiego komentarza.")
        db.run("UPDATE annotations SET resolved = 1 - resolved WHERE id = ?", comment_id)
        return review_json(f, user)

    @app.post("/api/files/{file_id}/status")
    async def api_status(request: Request, file_id: int):
        user, f = review_file(request, file_id)
        try:
            status = str((await request.json()).get("status", ""))
        except ValueError:
            raise HTTPException(400, "Nieprawidłowe dane.")
        allowed = {"admin": {"review", "", "approved", "changes"}, "client": {"approved", "changes"}}[user["role"]]
        if status not in allowed:
            raise HTTPException(400, "Niedozwolona zmiana statusu.")
        db.run("UPDATE files SET review_status = ?, review_by = ?, review_at = ? WHERE id = ?",
               status, user["id"] if status else None, now_iso() if status else None, f["id"])
        f = db.one("SELECT * FROM files WHERE id = ?", f["id"])
        name = user["display_name"] or user["username"]
        text = {"approved": f"✅ {name} akceptuje „{f['original_name']}”.",
                "changes": f"✏️ {name} prosi o poprawki w „{f['original_name']}” — szczegóły w komentarzach do pliku.",
                "review": f"🔎 Do akceptacji: „{f['original_name']}”."}.get(status)
        if text:
            review_notify(f, user, text)
        db.log(user["id"], "review_" + (status or "cleared"), f["original_name"])
        return review_json(f, user)

    # ------------------------------------------------------------------ wiadomości na żywo i liczniki

    @app.get("/api/thread/{client_id}")
    def api_thread(request: Request, client_id: int, after: int = 0, visible: int = 1):
        user = current_user(request)
        if not user or user["must_change_password"]:
            raise HTTPException(401, "Zaloguj się ponownie.")
        if user["role"] != "admin" and sid(user) != client_id:
            raise HTTPException(403, "Brak dostępu.")
        rows = db.all(
            "SELECT m.*, u.display_name, u.username, u.role FROM messages m JOIN users u ON u.id = m.author_id"
            " WHERE m.client_id = ? AND m.id > ? ORDER BY m.id LIMIT 200", client_id, after,
        )
        if visible:
            mark_seen(user, client_id)
        return {"messages": [message_json(m, user) for m in rows], "unread": unread_for(user)}

    @app.get("/api/unread")
    def api_unread(request: Request):
        user = current_user(request)
        if not user:
            raise HTTPException(401, "Zaloguj się ponownie.")
        return {"unread": unread_for(user)}

    @app.post("/admin/test-email")
    def test_email(request: Request, csrf: str = Form("")):
        check_csrf(request, csrf)
        require_user(request, "admin")
        if not notify.configured(settings) or not settings.notify_email:
            flash(request, "Poczta nie jest skonfigurowana — uzupełnij BSK_SMTP_* i BSK_NOTIFY_EMAIL w pliku .env.", "error")
        else:
            err = notify.send(settings, settings.notify_email, "Test powiadomień",
                              ["Jeśli to czytasz, powiadomienia e-mail z portalu działają."],
                              settings.public_url + "/admin", "Otwórz panel", wait=True)
            if err:
                flash(request, f"Nie udało się wysłać e-maila: {err}", "error")
            else:
                flash(request, f"Wysłano e-mail testowy na {settings.notify_email}.")
        return RedirectResponse("/account", 303)

    # ------------------------------------------------------------------ wysyłka w kawałkach z przeglądarki

    def api_user(request: Request):
        user = current_user(request)
        if not user or user["must_change_password"]:
            raise HTTPException(401, "Zaloguj się ponownie.")
        if not security.csrf_ok(request.session.get("csrf"), request.headers.get("x-csrf-token")):
            raise HTTPException(400, "Sesja wygasła — odśwież stronę.")
        return user

    def browser_upload_row(user, upload_id: str):
        row = chunk_store.get(upload_id)
        if not row or row["user_id"] != user["id"]:
            raise HTTPException(404, "Wysyłka wygasła — zacznij od nowa.")
        return row

    @app.post("/api/uploads")
    async def browser_upload_init(request: Request):
        user = api_user(request)
        try:
            body = await request.json()
            name, size = str(body["name"]), int(body["size"])
            folder_path = clean_dir(str(body.get("dir") or ""))
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Nieprawidłowe dane.")
        if size < 0:
            raise HTTPException(400, "Nieprawidłowe dane.")
        if size > max_upload:
            raise HTTPException(413, f"Plik {name} przekracza limit {settings.max_upload_mb} MB.")
        owner = space_of(user)
        dest_json = ""
        if isinstance(body.get("dest"), dict):
            d = dict(body["dest"])
            try:
                d["mtime"] = float(body.get("mtime")) / 1000 if body.get("mtime") else None  # JS: milisekundy
            except (TypeError, ValueError):
                d["mtime"] = None
            dest_owner, dest_folder, _, _ = resolve_dest(user, d)
            exists, fid = find_folder_path(dest_owner, dest_folder, folder_path) if folder_path else (True, dest_folder)
            if exists and d["mtime"]:
                same = db.one("SELECT id FROM files WHERE client_id = ? AND folder_id IS ? AND deleted_at IS NULL"
                              " AND original_name = ? AND size = ? AND src_mtime IS NOT NULL AND abs(src_mtime - ?) < 2",
                              dest_owner["id"], fid, name, size, d["mtime"])
                if same:
                    return {"status": "exists", "file_id": same["id"]}  # już jest — wznowienie wysyłki folderu
            owner = dest_owner if dest_owner["role"] == "client" else owner
            dest_json = json.dumps(d)
        if user["role"] == "client" and owner["quota_mb"]:
            pending = db.one(
                "SELECT COALESCE(SUM(size), 0) AS s FROM uploads WHERE user_id = ?", user["id"]
            )["s"]
            if client_usage(owner["id"]) + pending + size > owner["quota_mb"] * 1024 * 1024:
                raise HTTPException(
                    413, f"Brak miejsca na koncie (limit {owner['quota_mb']} MB). Skontaktuj się z BSK Media."
                )
        row = chunk_store.create(name=name, size=size, user_id=user["id"], target=folder_path, dest=dest_json)
        if dest_json and row["status"] == "done":  # pusty plik — od razu na miejsce
            fid = finalize_upload(user, row)
            return {"id": row["id"], "status": "stored", "file_id": fid, "received": 0, "chunk_size": chunks.CHUNK_SIZE}
        return {"id": row["id"], "received": row["received"], "chunk_size": chunks.CHUNK_SIZE}

    @app.get("/api/uploads/{upload_id}")
    def browser_upload_status(request: Request, upload_id: str):
        row = browser_upload_row(api_user(request), upload_id)
        return {"id": row["id"], "received": row["received"], "size": row["size"], "status": row["status"]}

    @app.put("/api/uploads/{upload_id}")
    async def browser_upload_chunk(request: Request, upload_id: str, offset: int = Query(..., ge=0)):
        user = api_user(request)
        row = browser_upload_row(user, upload_id)
        try:
            row = await chunk_store.write(row, offset, request.stream())
        except chunks.ChunkError as exc:
            return JSONResponse({"detail": exc.detail, "received": exc.received}, status_code=exc.status)
        if row["status"] == "done" and row["dest"]:
            fid = await asyncio.to_thread(finalize_upload, user, row)
            return {"id": row["id"], "received": row["received"], "status": "stored", "file_id": fid}
        return {"id": row["id"], "received": row["received"], "status": row["status"]}

    @app.post("/api/uploads/finish")
    async def browser_upload_finish(request: Request):
        """Koniec wysyłki z przeglądarki: komentarz do rozmowy, powiadomienia, komunikat i adres powrotu."""
        user = api_user(request)
        try:
            body = await request.json()
            dest = dict(body.get("dest") or {})
            stored, skipped = int(body.get("stored", 0)), int(body.get("skipped", 0))
            failed = [str(x)[:200] for x in (body.get("failed") or [])][:50]
            names = [str(x)[:200] for x in (body.get("names") or [])][:20]
        except (ValueError, TypeError):
            raise HTTPException(400, "Nieprawidłowe dane.")
        owner, folder_id, note, review = resolve_dest(user, dest)
        who = user["display_name"] or user["username"]
        if note.strip() and stored:
            add_message(owner["id"] if owner["role"] == "client" else owner["id"], user, note)
        if stored and owner["role"] == "client":
            listing = ", ".join(names) + (f" i {stored - len(names)} innych" if stored > len(names) else "")
            if user["role"] == "client":
                notify_admin(settings, f"Nowe pliki od {owner['company'] or who}",
                             f"{who} wysłał(a) {stored} plików: {listing}" + (f"\n\nKomentarz: {note}" if note.strip() else ""),
                             link=f"{settings.public_url}/admin/clients/{owner['id']}", throttle_key=f"admin:{owner['id']}")
            else:
                if review:
                    add_message(owner["id"], user, "🔎 Do akceptacji: " + listing)
                notify_client(owner, "Nowe pliki od BSK Media", [f"Udostępniliśmy Ci plików: {stored}.", note.strip()])
        parts = [f"Zapisano plików: {stored}."]
        if skipped:
            parts.append(f"Pominięto {skipped} — już były na serwerze.")
        if failed:
            parts.append(f"Nie udało się wysłać {len(failed)}: " + ", ".join(failed[:10]) + ("…" if len(failed) > 10 else ""))
        flash(request, " ".join(parts), "warn" if failed else "ok")
        db.log(user["id"], "upload_batch", f"{stored} zapisanych, {skipped} pominiętych, {len(failed)} błędów")
        return {"redirect": space_url(user, owner["id"], folder_id)}

    # ------------------------------------------------------------------ transfery (jak WeTransfer)

    BG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
    EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")

    def backgrounds() -> list[str]:
        d = settings.backgrounds_dir
        return sorted(p.name for p in d.iterdir() if p.suffix.lower() in BG_EXT) if d.is_dir() else []

    def transfer_files_of(transfer_id: int):
        return db.all("SELECT * FROM transfer_files WHERE transfer_id = ? ORDER BY id", transfer_id)

    def transfer_url(t) -> str:
        return f"{settings.public_url}/t/{t['token']}"

    def purge_old_transfers() -> None:
        """Pliki wygasłych transferów usuwamy 7 dni po terminie (zostaje historia na liście)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        for t in db.all("SELECT * FROM transfers WHERE files_deleted = 0 AND (expires_at < ? OR revoked = 1)", cutoff):
            if t["revoked"] or t["expires_at"] < cutoff:
                shutil.rmtree(settings.transfers_dir / str(t["id"]), ignore_errors=True)
                db.run("UPDATE transfers SET files_deleted = 1 WHERE id = ?", t["id"])

    @app.get("/admin/transfers", response_class=HTMLResponse)
    def transfers_page(request: Request):
        require_user(request, "admin")
        purge_old_transfers()
        rows = db.all(
            "SELECT t.*, (SELECT COUNT(*) FROM transfer_files f WHERE f.transfer_id = t.id) AS n_files,"
            " (SELECT COALESCE(SUM(size), 0) FROM transfer_files f WHERE f.transfer_id = t.id) AS total"
            " FROM transfers t ORDER BY t.id DESC LIMIT 300")
        return render(request, "admin_transfers.html", transfers=rows, now=now_iso(), nav="transfers",
                      backgrounds=backgrounds(), public_url=settings.public_url, mail_ok=notify.configured(settings))

    @app.post("/admin/transfers")
    def create_transfer(
        request: Request,
        files: list[UploadFile] | None = File(None),
        upload_ids: list[str] = Form([]),
        title: str = Form(""),
        message: str = Form(""),
        recipients: str = Form(""),
        expires_days: int = Form(7),
        password: str = Form(""),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        sources = incoming(admin, files, upload_ids)
        if not sources:
            raise HTTPException(400, "Dodaj co najmniej jeden plik.")
        emails = [e.strip() for e in re.split(r"[,;\s]+", recipients) if e.strip()]
        bad = [e for e in emails if not EMAIL_RE.match(e)]
        if bad:
            raise HTTPException(400, "Nieprawidłowy adres e-mail: " + ", ".join(bad))
        days = max(1, min(int(expires_days or 7), 365))
        token = secrets.token_urlsafe(16)
        tid = db.run(
            "INSERT INTO transfers (token, title, message, recipients, password_hash, created_by, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", token, title.strip()[:150], message.strip()[:3000], ", ".join(emails[:30]),
            security.hash_password(password.strip()) if password.strip() else "", admin["id"], now_iso(), iso_in(days))
        tdir = settings.transfers_dir / str(tid)
        for src in sources:
            if (src.filename or "").casefold() in SKIP_NAMES:
                continue
            sub = getattr(src, "subdir", "")
            dest = storage.unique_path(tdir / sub if sub else tdir, storage.safe_name(src.filename or "plik"))
            try:
                size, sha = place(src, dest, max_upload)
            except storage.TooLarge:
                raise HTTPException(413, f"Plik przekracza limit {settings.max_upload_mb} MB.")
            display = f"{sub}/{src.filename or dest.name}" if sub else (src.filename or dest.name)
            fid = db.run("INSERT INTO transfer_files (transfer_id, name, rel_path, size, sha256) VALUES (?, ?, ?, ?, ?)",
                         tid, display, dest.relative_to(settings.transfers_dir).as_posix(), size, sha)
            thumbs.generate_async(dest, settings.thumbs_dir / "transfery", fid, sha)
        t = db.one("SELECT * FROM transfers WHERE id = ?", tid)
        tf = transfer_files_of(tid)
        total = sum(f["size"] for f in tf)
        sent = 0
        for e in emails:
            notify.send(settings, e, t["title"] or "BSK Media przesyła Ci pliki",
                        [t["message"], f"{len(tf)} {'plik' if len(tf) == 1 else 'pliki/plików'} · {storage.human_size(total)}"
                         f" · link ważny do {_local_dt(t['expires_at'])[:10]}"
                         + (" · plik chroniony hasłem (przekażemy je osobno)" if t["password_hash"] else "")],
                        transfer_url(t), "Pobierz pliki")
            sent += 1
        db.log(admin["id"], "transfer_created", f"{t['title'] or token[:6]}: {len(tf)} plików, odbiorcy: {t['recipients']}")
        request.session["new_link"] = [transfer_url(t)]
        if sent and notify.configured(settings):
            flash(request, f"Wysłano e-mail z linkiem do: {t['recipients']}.")
        elif emails:
            flash(request, "Poczta nie jest skonfigurowana — wyślij link odbiorcy samodzielnie.", "warn")
        return RedirectResponse("/admin/transfers", 303)

    def transfer_admin_or_404(transfer_id: int):
        t = db.one("SELECT * FROM transfers WHERE id = ?", transfer_id)
        if not t:
            raise HTTPException(404, "Nie ma takiego transferu.")
        return t

    @app.post("/admin/transfers/{transfer_id}/revoke")
    def revoke_transfer(request: Request, transfer_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        t = transfer_admin_or_404(transfer_id)
        db.run("UPDATE transfers SET revoked = 1 WHERE id = ?", transfer_id)
        db.log(admin["id"], "transfer_revoked", t["title"] or t["token"][:6])
        flash(request, "Transfer wyłączony — link już nie działa.")
        return RedirectResponse("/admin/transfers", 303)

    @app.post("/admin/transfers/{transfer_id}/delete")
    def delete_transfer(request: Request, transfer_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        t = transfer_admin_or_404(transfer_id)
        for f in transfer_files_of(transfer_id):
            thumbs.remove(settings.thumbs_dir / "transfery", f["id"])
        shutil.rmtree(settings.transfers_dir / str(transfer_id), ignore_errors=True)
        db.run("DELETE FROM transfers WHERE id = ?", transfer_id)
        db.log(admin["id"], "transfer_deleted", t["title"] or t["token"][:6])
        flash(request, "Transfer usunięty razem z plikami.")
        return RedirectResponse("/admin/transfers", 303)

    @app.post("/admin/transfers/backgrounds")
    def upload_background(request: Request, files: list[UploadFile] | None = File(None),
                          upload_ids: list[str] = Form([]), csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        added = 0
        for src in incoming(admin, files, upload_ids):
            name = storage.safe_name(src.filename or "tlo.jpg")
            if Path(name).suffix.lower() not in BG_EXT:
                continue
            dest = storage.unique_path(settings.backgrounds_dir, name)
            place(src, dest, 50 * 1024 * 1024)
            added += 1
        flash(request, f"Dodano tła: {added}." if added else "Dodaj zdjęcia JPG, PNG lub WEBP.", "ok" if added else "error")
        return RedirectResponse("/admin/transfers#tla", 303)

    @app.post("/admin/transfers/backgrounds/{name}/delete")
    def delete_background(request: Request, name: str, csrf: str = Form("")):
        check_csrf(request, csrf)
        require_user(request, "admin")
        if name in backgrounds():
            (settings.backgrounds_dir / name).unlink(missing_ok=True)
        return RedirectResponse("/admin/transfers#tla", 303)

    @app.get("/t/tlo/{name}")
    def background_image(name: str):
        if name not in backgrounds():
            raise HTTPException(404, "Brak tła.")
        return FileResponse(settings.backgrounds_dir / name, headers={"Cache-Control": "public, max-age=86400"})

    def public_transfer(token: str):
        """Transfer dla odbiorcy (bez logowania). Zwraca (transfer, problem)."""
        t = db.one("SELECT * FROM transfers WHERE token = ?", token)
        if not t:
            raise HTTPException(404, "Nie ma takiego transferu.")
        if t["revoked"] or t["files_deleted"]:
            return t, "Ten transfer został wyłączony albo usunięty."
        if t["expires_at"] <= now_iso():
            return t, "Ten transfer wygasł. Poproś BSK Media o nowy link."
        return t, None

    def transfer_unlocked(request: Request, t) -> bool:
        return not t["password_hash"] or t["id"] in request.session.get("transfers_ok", [])

    def transfer_ready(request: Request, token: str):
        t, problem = public_transfer(token)
        if problem:
            raise HTTPException(410, problem)
        if not transfer_unlocked(request, t):
            raise Redirect(f"/t/{token}")
        return t

    def count_download(request: Request, t, what: str) -> None:
        rng = request.headers.get("range", "")
        if rng and not rng.replace(" ", "").startswith("bytes=0-"):
            return  # wznowienie przerwanego pobierania — nie liczymy drugi raz
        db.run("UPDATE transfers SET downloads = downloads + 1, last_download = ? WHERE id = ?", now_iso(), t["id"])
        db.log(None, "transfer_download", f"{t['title'] or t['token'][:6]}: {what} z {client_ip(request)}")
        notify_admin(settings, f"Pobrano transfer: {t['title'] or 'bez tytułu'}",
                     f"Odbiorca pobrał: {what}." + (f"\n\nOdbiorcy: {t['recipients']}" if t["recipients"] else ""),
                     link=f"{settings.public_url}/admin/transfers", throttle_key=f"transfer:{t['id']}")

    @app.get("/t/{token}", response_class=HTMLResponse)
    def transfer_page(request: Request, token: str):
        t, problem = public_transfer(token)
        tf = [] if problem else transfer_files_of(t["id"])
        return render(request, "transfer.html", user=None, t=t, problem=problem, files=tf, token=token,
                      unlocked=transfer_unlocked(request, t), total=sum(f["size"] for f in tf),
                      background=transfers.pick_background(token, backgrounds()))

    @app.post("/t/{token}")
    def transfer_unlock(request: Request, token: str, password: str = Form(""), csrf: str = Form("")):
        check_csrf(request, csrf)
        t, problem = public_transfer(token)
        ip = client_ip(request)
        if problem:
            return RedirectResponse(f"/t/{token}", 303)
        if limiter.blocked("transfer:" + token, ip):
            flash(request, "Zbyt wiele prób. Spróbuj za 15 minut.", "error")
        elif t["password_hash"] and security.verify_password(password, t["password_hash"]):
            request.session["transfers_ok"] = (request.session.get("transfers_ok", []) + [t["id"]])[-20:]
        else:
            limiter.fail("transfer:" + token, ip)
            flash(request, "Nieprawidłowe hasło.", "error")
        return RedirectResponse(f"/t/{token}", 303)

    def transfer_file_or_404(t, file_id: int):
        f = db.one("SELECT * FROM transfer_files WHERE id = ? AND transfer_id = ?", file_id, t["id"])
        path = settings.transfers_dir / f["rel_path"] if f else None
        if not f or not storage.inside(settings.transfers_dir, path) or not path.is_file():
            raise HTTPException(404, "Nie ma takiego pliku.")
        return f, path

    @app.get("/t/{token}/f/{file_id}")
    def transfer_download(request: Request, token: str, file_id: int):
        t = transfer_ready(request, token)
        f, path = transfer_file_or_404(t, file_id)
        count_download(request, t, f["name"])
        return FileResponse(path, filename=f["name"].rsplit("/", 1)[-1])

    @app.get("/t/{token}/thumb/{file_id}")
    def transfer_thumb(request: Request, token: str, file_id: int, s: str = "thumb"):
        t = transfer_ready(request, token)
        f, path = transfer_file_or_404(t, file_id)
        size = "preview" if s == "preview" else "thumb"
        cdir = settings.thumbs_dir / "transfery"
        cached = thumbs.cache_path(cdir, f["id"], f["sha256"], size)
        if not cached.exists() and not thumbs.generate(path, cdir, f["id"], f["sha256"]):
            raise HTTPException(404, "Brak podglądu.")
        return FileResponse(cached, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/t/{token}/zip")
    def transfer_zip(request: Request, token: str):
        t = transfer_ready(request, token)
        entries = []
        for f in transfer_files_of(t["id"]):
            path = settings.transfers_dir / f["rel_path"]
            if storage.inside(settings.transfers_dir, path) and path.is_file():
                entries.append((storage.safe_rel_path(f["name"]), path))
        if not entries:
            raise HTTPException(404, "Brak plików.")
        count_download(request, t, f"wszystkie pliki (ZIP, {len(entries)})")
        name = storage.safe_name((t["title"] or "BSK Media - pliki"))[:80] + ".zip"
        return StreamingResponse(transfers.zip_stream(entries), media_type="application/zip", headers={
            "Content-Disposition": f"attachment; filename=\"pliki.zip\"; filename*=UTF-8''{quote(name)}",
            "Cache-Control": "no-store"})

    # ------------------------------------------------------------------ API agenta kopii zapasowych

    def device_from_token(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(401, "Brak tokenu urządzenia.")
        device = db.one("SELECT * FROM devices WHERE token_hash = ?", security.token_hash(auth[7:].strip()))
        if not device or not device["active"]:
            raise HTTPException(401, "Token nieprawidłowy albo urządzenie wyłączone.")
        version = agents.version_from_ua(request.headers.get("user-agent", "")) or device["agent_version"]
        db.run("UPDATE devices SET last_seen = ?, last_ip = ?, agent_version = ? WHERE id = ?", now_iso(),
               client_ip(request), version, device["id"])
        return db.one("SELECT * FROM devices WHERE id = ?", device["id"])

    @app.get("/api/agent/config")
    def agent_config(request: Request):
        """Ustawienia z panelu: foldery (pusta lista = ustawienia lokalne), synchronizacja, aktualizacja."""
        device = device_from_token(request)
        return {"folders": [ln for ln in device["folders"].splitlines() if ln.strip()],
                "lan_server": settings.lan_url,
                "sync": device["sync_requested"] if device["sync_requested"] != device["sync_done"] else "",
                "update": agents.update_offer(settings, device["agent_version"])}

    @app.post("/api/agent/report")
    async def agent_report(request: Request):
        """Stan programu na komputerze: kolejka, błędy, dziennik, wykonana synchronizacja."""
        device = device_from_token(request)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Nieprawidłowy raport.")
        if not isinstance(body, dict):
            raise HTTPException(400, "Nieprawidłowy raport.")
        report = agents.clean_report(body)
        sync_done = str(body.get("sync_done") or "")[:40]
        if sync_done and sync_done == device["sync_requested"]:
            db.run("UPDATE devices SET sync_done = ? WHERE id = ?", sync_done, device["id"])
        db.run("UPDATE devices SET agent_report = ?, agent_report_at = ? WHERE id = ?",
               json.dumps(report, ensure_ascii=False), now_iso(), device["id"])
        return {"ok": True}

    @app.get("/api/agent/update/{name}")
    def agent_update_file(request: Request, name: str):
        """Nowa wersja programu z bsk-dane/instalator (tylko dla zalogowanego tokenem komputera)."""
        device_from_token(request)
        if name not in agents.UPDATE_FILES or not (settings.installer_dir / name).is_file():
            raise HTTPException(404, "Brak pliku aktualizacji.")
        return FileResponse(settings.installer_dir / name, media_type="application/octet-stream", filename=name)

    @app.get("/api/agent/ping")
    def agent_ping(request: Request):
        device = device_from_token(request)
        return {"ok": True, "device": device["name"], "max_file_mb": settings.max_backup_file_mb}

    @app.get("/api/agent/status")
    def agent_status(request: Request, path: str, sha256: str):
        device = device_from_token(request)
        try:
            rel = storage.safe_rel_path(path)
        except ValueError:
            raise HTTPException(400, "Nieprawidłowa ścieżka.")
        row = db.one("SELECT sha256 FROM backup_files WHERE device_id = ? AND rel_path = ?", device["id"], rel)
        return {"needed": not row or row["sha256"] != sha256.lower()}

    def agent_target(device, path: str):
        try:
            base, target = backup_path(device, path)
        except ValueError:
            raise HTTPException(400, "Nieprawidłowa ścieżka.")
        return base, target, target.relative_to(base).as_posix()

    def backup_limit(device, rel: str) -> int:
        """Ile bajtów może mieć plik, nie przekraczając limitu miejsca komputera."""
        if not device["quota_mb"]:
            return max_backup
        used = db.one(
            "SELECT COALESCE(SUM(size), 0) AS used FROM backup_files WHERE device_id = ? AND rel_path != ?",
            device["id"], rel,
        )["used"]
        return min(max_backup, device["quota_mb"] * 1024 * 1024 - used)

    def check_size(device, rel: str, size: int) -> None:
        limit = backup_limit(device, rel)
        if size > limit:
            if limit < max_backup:
                raise HTTPException(507, "Przekroczono limit miejsca na kopie tego komputera.")
            raise HTTPException(413, "Plik przekracza limit.")

    def unchanged(device, target: Path, rel: str, sha256: str) -> bool:
        existing = db.one("SELECT sha256 FROM backup_files WHERE device_id = ? AND rel_path = ?", device["id"], rel)
        return bool(existing and existing["sha256"] == sha256 and target.is_file())

    def store_backup(device, base: Path, target: Path, rel: str, src: str, size: int, sha256: str, mtime: float):
        """Przenosi gotowy plik na miejsce (stara wersja -> .wersje/) i zapisuje w bazie."""
        target.parent.mkdir(parents=True, exist_ok=True)
        versions = 1
        if target.is_file():
            vdir = (base / ".wersje" / rel).parent
            vdir.mkdir(parents=True, exist_ok=True)
            os.replace(target, vdir / f"{target.name}.{storage.version_stamp()}")
            versions = 1 + storage.prune_versions(vdir, target.name, settings.backup_keep_versions)
        os.replace(src, target)
        if mtime > 0:
            os.utime(target, (mtime, mtime))
        db.run(
            "INSERT INTO backup_files (device_id, rel_path, size, sha256, mtime, uploaded_at, versions)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (device_id, rel_path) DO UPDATE SET"
            " size = excluded.size, sha256 = excluded.sha256, mtime = excluded.mtime,"
            " uploaded_at = excluded.uploaded_at, versions = excluded.versions",
            device["id"], rel, size, sha256, mtime, now_iso(), versions,
        )
        return {"status": "stored", "path": rel, "size": size, "versions": versions}

    @app.put("/api/agent/files")
    async def agent_upload(
        request: Request,
        path: str = Query(...),
        sha256: str = Query(..., pattern="^[0-9a-fA-F]{64}$"),
        mtime: float = Query(0),
    ):
        """Wysyłka małego pliku w jednym żądaniu."""
        device = device_from_token(request)
        base, target, rel = agent_target(device, path)
        sha256 = sha256.lower()
        if unchanged(device, target, rel, sha256):
            return {"status": "unchanged", "path": rel}
        limit = backup_limit(device, rel)
        length = request.headers.get("content-length", "")
        check_size(device, rel, int(length) if length.isdigit() else 0)
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=settings.tmp_dir, prefix="agent-")
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        check_size(device, rel, size)
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != sha256:
                raise HTTPException(409, "Suma kontrolna się nie zgadza (plik zmienił się w trakcie wysyłki).")
            return store_backup(device, base, target, rel, tmp_name, size, sha256, mtime)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    @app.post("/api/agent/uploads")
    async def agent_upload_init(request: Request):
        """Początek (lub wznowienie) wysyłki dużego pliku w kawałkach."""
        device = device_from_token(request)
        try:
            body = await request.json()
            path, size = str(body["path"]), int(body["size"])
            sha256, mtime = str(body["sha256"]).lower(), float(body.get("mtime") or 0)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Nieprawidłowe dane.")
        if size < 0 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise HTTPException(400, "Nieprawidłowe dane.")
        base, target, rel = agent_target(device, path)
        if unchanged(device, target, rel, sha256):
            return {"status": "unchanged", "path": rel}
        check_size(device, rel, size)
        row = chunk_store.find_resumable(device["id"], rel, sha256)
        if not row or row["size"] != size:
            row = chunk_store.create(name=target.name, size=size, device_id=device["id"],
                                     expected_sha256=sha256, target=rel, mtime=mtime)
        return {"status": "upload", "id": row["id"], "received": row["received"], "chunk_size": chunks.CHUNK_SIZE}

    def agent_upload_row(device, upload_id: str):
        row = chunk_store.get(upload_id)
        if not row or row["device_id"] != device["id"]:
            raise HTTPException(404, "Wysyłka wygasła — zacznij od nowa.")
        return row

    @app.get("/api/agent/uploads/{upload_id}")
    def agent_upload_status(request: Request, upload_id: str):
        row = agent_upload_row(device_from_token(request), upload_id)
        return {"id": row["id"], "received": row["received"], "size": row["size"]}

    @app.put("/api/agent/uploads/{upload_id}")
    async def agent_upload_chunk(request: Request, upload_id: str, offset: int = Query(..., ge=0)):
        device = device_from_token(request)
        row = agent_upload_row(device, upload_id)
        try:
            row = await chunk_store.write(row, offset, request.stream())
        except chunks.ChunkError as exc:
            return JSONResponse({"detail": exc.detail, "received": exc.received}, status_code=exc.status)
        if row["status"] != "done":
            return {"status": "partial", "received": row["received"]}
        base, target, rel = agent_target(device, row["target"])
        result = store_backup(device, base, target, rel, str(chunk_store.path(row["id"])), row["size"],
                              row["sha256"], row["mtime"])
        chunk_store.remove(row["id"])
        return result

    # ------------------------------------------------------------------ błędy

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/") or request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", status_code=exc.status_code, message=exc.detail)

    app.state.quote = quote
    templates.env.filters["q"] = lambda v: quote(v or "", safe="")
    return app


def _bootstrap_admin(db: Database, settings: Settings) -> None:
    """Przy pierwszym uruchomieniu zakłada konto administratora z BSK_ADMIN_USERNAME/PASSWORD."""
    if db.one("SELECT 1 FROM users WHERE role = 'admin'"):
        return
    if not (settings.admin_username and settings.admin_password):
        log.warning("Brak konta administratora — ustaw BSK_ADMIN_USERNAME i BSK_ADMIN_PASSWORD i uruchom ponownie.")
        return
    if problem := security.password_problem(settings.admin_password):
        raise RuntimeError(f"BSK_ADMIN_PASSWORD: {problem}")
    db.run(
        "INSERT INTO users (username, password_hash, role, display_name, company, created_at)"
        " VALUES (?, ?, 'admin', 'Administrator', 'BSK Media', ?)",
        settings.admin_username, security.hash_password(settings.admin_password), now_iso(),
    )
    db.log(None, "admin_bootstrapped", settings.admin_username)
    log.warning("Utworzono konto administratora '%s'. Możesz usunąć BSK_ADMIN_PASSWORD z .env.", settings.admin_username)
