"""BSK Media Cloud — portal plików dla klientów + odbiór kopii zapasowych z komputerów pracowników."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import security, storage
from .config import Settings
from .db import Database, now_iso
from .notify import notify_admin

log = logging.getLogger("bskcloud")
BASE = Path(__file__).parent
USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,40}$")


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    for d in (settings.clients_dir, settings.backups_dir, settings.tmp_dir):
        d.mkdir(parents=True, exist_ok=True)
    # Duże uploady z formularza trafiają najpierw do katalogu tymczasowego — trzymamy go na dysku NAS.
    os.environ["TMPDIR"] = str(settings.tmp_dir)
    tempfile.tempdir = str(settings.tmp_dir)

    db = Database(settings.db_path)
    limiter = security.LoginLimiter()
    _bootstrap_admin(db, settings)

    app = FastAPI(title=settings.site_name, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    templates = Jinja2Templates(directory=str(BASE / "templates"))
    templates.env.filters["size"] = storage.human_size
    templates.env.filters["dt"] = lambda v: (v or "")[:16].replace("T", " ")
    templates.env.globals["site_name"] = settings.site_name
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

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
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        )
        if path.startswith(("/files", "/admin", "/account")):
            response.headers.setdefault("Cache-Control", "no-store")
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
        ctx["csrf"] = csrf_token(request)
        ctx["flashes"] = request.session.pop("flash", [])
        ctx["max_upload_mb"] = settings.max_upload_mb
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def client_or_404(client_id: int):
        c = db.one("SELECT * FROM users WHERE id = ? AND role = 'client'", client_id)
        if not c:
            raise HTTPException(404, "Nie ma takiego klienta.")
        return c

    def client_dir(client) -> Path:
        return settings.clients_dir / storage.safe_slug(client["username"])

    def save_upload(client, uploader, upload: UploadFile, note: str) -> int:
        target_dir = client_dir(client)
        if uploader["role"] == "admin":
            target_dir = target_dir / "od BSK Media"
        name = storage.safe_name(upload.filename or "plik")
        dest = storage.unique_path(target_dir, name)
        try:
            size, sha = storage.copy_hashed(upload.file, dest, settings.tmp_dir, max_upload)
        except storage.TooLarge:
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
        return render(request, "account.html", user=user)

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
        return render(request, "client_files.html", files=client_files(user["id"]), messages=thread(user["id"]))

    @app.post("/files/upload")
    def client_upload(
        request: Request,
        files: list[UploadFile] = File(...),
        note: str = Form(""),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        user = require_user(request, "client")
        names = []
        for up in files:
            if up.filename:
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
        clients = db.all(
            "SELECT u.*, (SELECT COUNT(*) FROM files f WHERE f.client_id = u.id) AS file_count,"
            " (SELECT COALESCE(SUM(size), 0) FROM files f WHERE f.client_id = u.id) AS total_size,"
            " (SELECT MAX(uploaded_at) FROM files f WHERE f.client_id = u.id) AS last_upload,"
            " (SELECT MAX(created_at) FROM messages m WHERE m.client_id = u.id) AS last_message"
            " FROM users u WHERE u.role = 'client' ORDER BY u.active DESC, u.company, u.display_name"
        )
        recent = db.all(
            "SELECT f.*, u.display_name, u.company, u.username FROM files f JOIN users u ON u.id = f.client_id"
            " ORDER BY f.uploaded_at DESC LIMIT 15"
        )
        devices = db.all(
            "SELECT d.*, (SELECT COUNT(*) FROM backup_files b WHERE b.device_id = d.id) AS file_count,"
            " (SELECT COALESCE(SUM(size), 0) FROM backup_files b WHERE b.device_id = d.id) AS total_size"
            " FROM devices d ORDER BY d.active DESC, d.name"
        )
        return render(request, "admin_home.html", clients=clients, recent=recent, devices=devices)

    @app.get("/admin/clients/new", response_class=HTMLResponse)
    def new_client_form(request: Request):
        require_user(request, "admin")
        return render(request, "admin_client_new.html", suggested=security.generate_password())

    @app.post("/admin/clients/new")
    def create_client(
        request: Request,
        username: str = Form(...),
        display_name: str = Form(""),
        company: str = Form(""),
        email: str = Form(""),
        password: str = Form(...),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
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
            " must_change_password, created_at) VALUES (?, ?, 'client', ?, ?, ?, 1, ?)",
            username, security.hash_password(password), display_name.strip()[:120],
            company.strip()[:120], email.strip()[:200], now_iso(),
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
            folder=client_dir(client).as_posix().replace(settings.data_dir.as_posix(), "", 1),
        )

    @app.post("/admin/clients/{client_id}/edit")
    def edit_client(
        request: Request,
        client_id: int,
        display_name: str = Form(""),
        company: str = Form(""),
        email: str = Form(""),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        client_or_404(client_id)
        db.run(
            "UPDATE users SET display_name = ?, company = ?, email = ? WHERE id = ?",
            display_name.strip()[:120], company.strip()[:120], email.strip()[:200], client_id,
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
        files: list[UploadFile] = File(...),
        note: str = Form(""),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        client = client_or_404(client_id)
        count = 0
        for up in files:
            if up.filename:
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
        return render(request, "admin_device_new.html")

    @app.post("/admin/devices/new")
    def create_device(
        request: Request,
        name: str = Form(...),
        employee: str = Form(""),
        csrf: str = Form(""),
    ):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
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
            "INSERT INTO devices (name, employee, folder_name, token_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            name, employee.strip()[:120], folder, security.token_hash(token), now_iso(),
        )
        (settings.backups_dir / folder).mkdir(parents=True, exist_ok=True)
        db.log(admin["id"], "device_created", name)
        request.session["new_token"] = token
        return RedirectResponse(f"/admin/devices/{did}", 303)

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
        return render(
            request, "admin_device.html", device=device, files=rows[:per_page], has_more=len(rows) > per_page,
            page=page, q=q, stats=stats, token=request.session.pop("new_token", None),
            public_url=settings.public_url,
        )

    @app.post("/admin/devices/{device_id}/token")
    def rotate_token(request: Request, device_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        admin = require_user(request, "admin")
        device = device_or_404(device_id)
        token = security.new_device_token()
        db.run("UPDATE devices SET token_hash = ?, active = 1 WHERE id = ?", security.token_hash(token), device_id)
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
        return render(request, "admin_versions.html", device=device, rel=rel, versions=versions, current=current)

    @app.get("/admin/log", response_class=HTMLResponse)
    def admin_log(request: Request):
        require_user(request, "admin")
        events = db.all(
            "SELECT e.*, u.username FROM events e LEFT JOIN users u ON u.id = e.user_id ORDER BY e.id DESC LIMIT 300"
        )
        return render(request, "admin_log.html", events=events)

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

    @app.put("/api/agent/files")
    async def agent_upload(
        request: Request,
        path: str = Query(...),
        sha256: str = Query(..., pattern="^[0-9a-fA-F]{64}$"),
        mtime: float = Query(0),
    ):
        device = device_from_token(request)
        try:
            base, target = backup_path(device, path)
        except ValueError:
            raise HTTPException(400, "Nieprawidłowa ścieżka.")
        rel = target.relative_to(base).as_posix()
        sha256 = sha256.lower()
        existing = db.one("SELECT * FROM backup_files WHERE device_id = ? AND rel_path = ?", device["id"], rel)
        if existing and existing["sha256"] == sha256 and target.is_file():
            return {"status": "unchanged", "path": rel}

        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=settings.tmp_dir, prefix="agent-")
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_backup:
                        raise HTTPException(413, "Plik przekracza limit.")
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != sha256:
                raise HTTPException(409, "Suma kontrolna się nie zgadza (plik zmienił się w trakcie wysyłki).")
            target.parent.mkdir(parents=True, exist_ok=True)
            versions = 1
            if target.is_file():
                vdir = (base / ".wersje" / rel).parent
                vdir.mkdir(parents=True, exist_ok=True)
                os.replace(target, vdir / f"{target.name}.{storage.version_stamp()}")
                versions = 1 + storage.prune_versions(vdir, target.name, settings.backup_keep_versions)
            os.replace(tmp_name, target)
            if mtime > 0:
                os.utime(target, (mtime, mtime))
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        db.run(
            "INSERT INTO backup_files (device_id, rel_path, size, sha256, mtime, uploaded_at, versions)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (device_id, rel_path) DO UPDATE SET"
            " size = excluded.size, sha256 = excluded.sha256, mtime = excluded.mtime,"
            " uploaded_at = excluded.uploaded_at, versions = excluded.versions",
            device["id"], rel, size, sha256, mtime, now_iso(), versions,
        )
        return {"status": "stored", "path": rel, "size": size, "versions": versions}

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
