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
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from . import backup, charts, chunks, office, security, storage
from .config import Settings
from .db import Database, iso_in, now_iso
from .notify import notify_admin

log = logging.getLogger("bskcloud")
BASE = Path(__file__).parent
USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,40}$")
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
        if path.startswith(("/s/", "/instaluj")):
            response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="bsk_session",
        max_age=settings.session_hours * 3600,
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
        if not user or not user["active"] or user["session_version"] != request.session.get("sv"):
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
        ctx["csrf"] = csrf_token(request)
        ctx["flashes"] = request.session.pop("flash", [])
        ctx["max_upload_mb"] = settings.max_upload_mb
        ctx["new_links"] = request.session.pop("new_link", None)
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def client_or_404(client_id: int):
        c = db.one("SELECT * FROM users WHERE id = ? AND role = 'client'", client_id)
        if not c:
            raise HTTPException(404, "Nie ma takiego klienta.")
        return c

    def client_dir(client) -> Path:
        return settings.clients_dir / storage.safe_slug(client["username"])

    def client_usage(client_id: int) -> int:
        """Miejsce zajęte przez pliki wysłane przez samego klienta (to, co udostępnia admin, się nie liczy)."""
        row = db.one(
            "SELECT COALESCE(SUM(size), 0) AS used FROM files WHERE client_id = ? AND uploaded_by = ?",
            client_id, client_id,
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

    def save_upload(client, uploader, upload, note: str) -> int:
        limit = max_upload
        quota_exceeded = False
        if uploader["role"] == "client" and client["quota_mb"]:
            remaining = client["quota_mb"] * 1024 * 1024 - client_usage(client["id"])
            if remaining < limit:
                limit, quota_exceeded = max(0, remaining), True
        target_dir = client_dir(client)
        if uploader["role"] == "admin":
            target_dir = target_dir / "od BSK Media"
        name = storage.safe_name(upload.filename or "plik")
        dest = storage.unique_path(target_dir, name)
        try:
            size, sha = place(upload, dest, limit)
        except storage.TooLarge:
            if quota_exceeded:
                raise HTTPException(
                    413, f"Brak miejsca na koncie (limit {client['quota_mb']} MB). Skontaktuj się z BSK Media."
                )
            raise HTTPException(413, f"Plik przekracza limit {settings.max_upload_mb} MB.")
        rel = dest.relative_to(settings.clients_dir).as_posix()
        fid = db.run(
            "INSERT INTO files (client_id, rel_path, original_name, size, sha256, uploaded_by, uploaded_at, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            client["id"], rel, upload.filename or name, size, sha, uploader["id"], now_iso(), note[:500],
        )
        db.log(uploader["id"], "upload", f"{rel} ({size} B)")
        return fid

    def file_for(user, file_id: int):
        f = db.one("SELECT * FROM files WHERE id = ?", file_id)
        if not f or (user["role"] != "admin" and f["client_id"] != user["id"]):
            raise HTTPException(404, "Nie znaleziono pliku.")
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

    def thread(client_id: int):
        return db.all(
            "SELECT m.*, u.display_name, u.username, u.role FROM messages m"
            " JOIN users u ON u.id = m.author_id WHERE m.client_id = ? ORDER BY m.created_at, m.id",
            client_id,
        )

    def client_files(client_id: int):
        return db.all(
            "SELECT f.*, u.role AS uploader_role FROM files f JOIN users u ON u.id = f.uploaded_by"
            " WHERE f.client_id = ? ORDER BY f.uploaded_at DESC, f.id DESC",
            client_id,
        )

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
               " FROM files f JOIN users u ON u.id = f.uploaded_by WHERE substr(f.uploaded_at, 1, 10) >= ?")
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
            "SELECT u.*, (SELECT COUNT(*) FROM files f WHERE f.client_id = u.id) AS file_count,"
            " (SELECT COALESCE(SUM(size), 0) FROM files f WHERE f.client_id = u.id) AS total_size,"
            " (SELECT COALESCE(SUM(size), 0) FROM files f WHERE f.client_id = u.id AND f.uploaded_by = u.id) AS own_size,"
            " (SELECT MAX(uploaded_at) FROM files f WHERE f.client_id = u.id) AS last_upload,"
            " (SELECT MAX(created_at) FROM messages m WHERE m.client_id = u.id) AS last_message"
            " FROM users u WHERE u.role = 'client' ORDER BY u.active DESC, u.company, u.display_name"
        )

    def online(last_seen: str | None, hours: int = 2) -> bool:
        if not last_seen:
            return False
        try:
            return datetime.now(timezone.utc) - datetime.fromisoformat(last_seen) < timedelta(hours=hours)
        except ValueError:
            return False

    templates.env.globals["online"] = online

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
    def login(request: Request, username: str = Form(...), password: str = Form(...), csrf: str = Form("")):
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
        request.session.update(uid=user["id"], sv=user["session_version"], csrf=security.new_csrf())
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
        return render(request, "account.html", user=user, nav="account")

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
    def my_files(request: Request):
        user = require_user(request, "client")
        files = client_files(user["id"])
        sent = [f for f in files if f["uploaded_by"] == user["id"]]
        received = [f for f in files if f["uploaded_by"] != user["id"]]
        return render(
            request, "client_files.html", files=files, messages=thread(user["id"]), quota=quota_info(user),
            days=daily_files(user["id"]), sent=sent, received=received, nav="files",
            sent_size=sum(f["size"] for f in sent), received_size=sum(f["size"] for f in received),
        )

    @app.post("/files/upload")
    def client_upload(
        request: Request,
        files: list[UploadFile] | None = File(None),
        upload_ids: list[str] = Form([]),
        note: str = Form(""),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        user = require_user(request, "client")
        names = []
        for up in incoming(user, files, upload_ids):
            save_upload(user, user, up, note)
            names.append(up.filename)
        if note.strip():
            add_message(user["id"], user, note)
        if names:
            flash(request, f"Wysłano plików: {len(names)}.")
            notify_admin(
                settings,
                f"Nowe pliki od {user['display_name'] or user['username']}",
                "Pliki:\n- " + "\n- ".join(names) + (f"\n\nKomentarz:\n{note}" if note.strip() else "")
                + f"\n\n{settings.public_url}/admin/clients/{user['id']}",
            )
        return RedirectResponse("/files", 303)

    @app.post("/files/message")
    def client_message(request: Request, body: str = Form(...), csrf: str = Form("")):
        check_csrf(request, csrf)
        user = require_user(request, "client")
        if body.strip():
            add_message(user["id"], user, body)
            notify_admin(
                settings,
                f"Wiadomość od {user['display_name'] or user['username']}",
                body + f"\n\n{settings.public_url}/admin/clients/{user['id']}",
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
        path.unlink(missing_ok=True)
        db.run("DELETE FROM files WHERE id = ?", file_id)
        db.log(user["id"], "delete", f["rel_path"])
        flash(request, f"Usunięto plik {f['original_name']}.")
        back = f"/admin/clients/{f['client_id']}" if user["role"] == "admin" else "/files"
        return RedirectResponse(back, 303)

    # ------------------------------------------------------------------ admin: klienci

    @app.get("/admin", response_class=HTMLResponse)
    def admin_home(request: Request):
        require_user(request, "admin")
        since = (date.today() - timedelta(days=29)).isoformat()
        kpi = {
            "clients": db.one("SELECT COUNT(*) AS n FROM users WHERE role = 'client' AND active = 1")["n"],
            "client_files_30": db.one(
                "SELECT COUNT(*) AS n, COALESCE(SUM(f.size), 0) AS b FROM files f JOIN users u ON u.id = f.uploaded_by"
                " WHERE u.role = 'client' AND substr(f.uploaded_at, 1, 10) >= ?", since),
            "storage": db.one("SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM files"),
            "backups": db.one("SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM backup_files"),
            "links": db.one(
                "SELECT COUNT(*) AS n, COALESCE(SUM(downloads), 0) AS d FROM share_links WHERE revoked = 0"
                " AND (expires_at IS NULL OR expires_at > ?)", now_iso()),
        }
        days = daily_files()
        clients = client_rows()
        top = sorted(clients, key=lambda c: c["total_size"], reverse=True)[:8]
        recent = db.all(
            "SELECT f.*, u.display_name, u.company, u.username, up.role AS uploader_role FROM files f"
            " JOIN users u ON u.id = f.client_id JOIN users up ON up.id = f.uploaded_by"
            " ORDER BY f.uploaded_at DESC LIMIT 8"
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
        return render(request, "admin_clients.html", clients=clients, q=q, nav="clients")

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
    def admin_client(request: Request, client_id: int):
        require_user(request, "admin")
        client = client_or_404(client_id)
        creds = request.session.pop("new_credentials", None)
        return render(
            request, "admin_client.html", client=client, files=client_files(client_id),
            messages=thread(client_id), credentials=creds, public_url=settings.public_url,
            quota=quota_info(client), days=daily_files(client_id), nav="clients",
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
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        client_or_404(client_id)
        quota_mb = parse_quota(quota_gb, quota_mb)
        db.run(
            "UPDATE users SET display_name = ?, company = ?, email = ?, quota_mb = ? WHERE id = ?",
            display_name.strip()[:120], company.strip()[:120], email.strip()[:200], max(0, quota_mb), client_id,
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
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        client = client_or_404(client_id)
        count = 0
        for up in incoming(admin, files, upload_ids):
            save_upload(client, admin, up, note)
            count += 1
        if note.strip():
            add_message(client_id, admin, note)
        flash(request, f"Udostępniono klientowi plików: {count}.")
        return RedirectResponse(f"/admin/clients/{client_id}", 303)

    @app.post("/admin/clients/{client_id}/message")
    def admin_message(request: Request, client_id: int, body: str = Form(...), csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        client_or_404(client_id)
        add_message(client_id, admin, body)
        return RedirectResponse(f"/admin/clients/{client_id}#wiadomosci", 303)

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

    @app.get("/admin/devices/{device_id}", response_class=HTMLResponse)
    def admin_device(request: Request, device_id: int, q: str = "", page: int = 1):
        require_user(request, "admin")
        device = device_or_404(device_id)
        page = max(1, page)
        per_page = 200
        like = f"%{q.strip()}%"
        rows = db.all(
            "SELECT * FROM backup_files WHERE device_id = ? AND rel_path LIKE ?"
            " ORDER BY uploaded_at DESC LIMIT ? OFFSET ?",
            device_id, like, per_page + 1, (page - 1) * per_page,
        )
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
            page=page, q=q, stats=stats, token=request.session.pop("new_token", None),
            public_url=settings.public_url, install_link=link, installer_ready=installer_ready(),
            days=daily_backups(device_id), nav="devices", special=SPECIAL_FOLDERS,
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
            f = db.one("SELECT rel_path FROM files WHERE id = ?", link["file_id"])
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
        f = db.one("SELECT * FROM files WHERE id = ?", file_id)
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
        fid = create_blank(user, user, kind, title)
        return RedirectResponse(f"/files/{fid}/edit" if office_ready() else "/files", 303)

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
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Nieprawidłowe dane.")
        if size < 0:
            raise HTTPException(400, "Nieprawidłowe dane.")
        if size > max_upload:
            raise HTTPException(413, f"Plik {name} przekracza limit {settings.max_upload_mb} MB.")
        if user["role"] == "client" and user["quota_mb"]:
            pending = db.one(
                "SELECT COALESCE(SUM(size), 0) AS s FROM uploads WHERE user_id = ?", user["id"]
            )["s"]
            if client_usage(user["id"]) + pending + size > user["quota_mb"] * 1024 * 1024:
                raise HTTPException(
                    413, f"Brak miejsca na koncie (limit {user['quota_mb']} MB). Skontaktuj się z BSK Media."
                )
        row = chunk_store.create(name=name, size=size, user_id=user["id"])
        return {"id": row["id"], "received": row["received"], "chunk_size": chunks.CHUNK_SIZE}

    @app.get("/api/uploads/{upload_id}")
    def browser_upload_status(request: Request, upload_id: str):
        row = browser_upload_row(api_user(request), upload_id)
        return {"id": row["id"], "received": row["received"], "size": row["size"], "status": row["status"]}

    @app.put("/api/uploads/{upload_id}")
    async def browser_upload_chunk(request: Request, upload_id: str, offset: int = Query(..., ge=0)):
        row = browser_upload_row(api_user(request), upload_id)
        try:
            row = await chunk_store.write(row, offset, request.stream())
        except chunks.ChunkError as exc:
            return JSONResponse({"detail": exc.detail, "received": exc.received}, status_code=exc.status)
        return {"id": row["id"], "received": row["received"], "status": row["status"]}

    # ------------------------------------------------------------------ API agenta kopii zapasowych

    def device_from_token(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(401, "Brak tokenu urządzenia.")
        device = db.one("SELECT * FROM devices WHERE token_hash = ?", security.token_hash(auth[7:].strip()))
        if not device or not device["active"]:
            raise HTTPException(401, "Token nieprawidłowy albo urządzenie wyłączone.")
        db.run("UPDATE devices SET last_seen = ?, last_ip = ? WHERE id = ?", now_iso(), client_ip(request), device["id"])
        return device

    @app.get("/api/agent/config")
    def agent_config(request: Request):
        """Foldery ustawione dla tego komputera w panelu (pusta lista = ustawienia lokalne agenta)."""
        device = device_from_token(request)
        return {"folders": [ln for ln in device["folders"].splitlines() if ln.strip()],
                "lan_server": settings.lan_url}

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
