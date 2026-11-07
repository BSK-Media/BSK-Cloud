import hashlib
import html
import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app import storage

ADMIN_PW = "AdminHaslo123"


@pytest.fixture()
def app(tmp_path):
    s = Settings(data_dir=tmp_path, secret_key="x" * 40, cookie_secure=False,
                 admin_username="admin", admin_password=ADMIN_PW, max_upload_mb=1, backup_keep_versions=2)
    return create_app(s)


def csrf_of(client, url):
    html = client.get(url).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, html[:500]
    return m.group(1)


def login(client, username, password):
    tok = csrf_of(client, "/login")
    return client.post("/login", data={"username": username, "password": password, "csrf": tok})


@pytest.fixture()
def admin(app):
    c = TestClient(app)
    login(c, "admin", ADMIN_PW)
    return c


def create_client_account(admin, username="jan.kowalski", pw="KlientHaslo1"):
    tok = csrf_of(admin, "/admin/clients/new")
    r = admin.post("/admin/clients/new", data={"username": username, "display_name": "Jan", "company": "ACME",
                                               "email": "", "password": pw, "csrf": tok})
    assert r.status_code == 200 and pw in r.text  # dane logowania pokazane raz
    return pw


def test_requires_login(app):
    c = TestClient(app)
    r = c.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_bad_password_and_csrf(app):
    c = TestClient(app)
    r = login(c, "admin", "zle")
    assert "Nieprawidłowy login" in r.text
    r = c.post("/login", data={"username": "admin", "password": ADMIN_PW, "csrf": "zly"})
    assert r.status_code == 400


def test_client_flow(app, admin):
    pw = create_client_account(admin)
    c = TestClient(app)
    r = login(c, "jan.kowalski", pw)
    assert "Zmiana hasła" in r.text  # wymuszona zmiana hasła
    tok = csrf_of(c, "/account")
    r = c.post("/account/password", data={"current": pw, "new": "NoweHaslo123", "repeat": "NoweHaslo123", "csrf": tok})
    assert "Wyślij pliki" in r.text

    tok = csrf_of(c, "/files")
    r = c.post("/files/upload", data={"csrf": tok, "note": "wersja 2"},
               files=[("files", ("../../etc/passwd logo.png", b"PNGDATA", "image/png"))])
    assert "Wysłano plików: 1" in r.text
    assert (app.state.settings.clients_dir / "jan.kowalski" / "passwd logo.png").read_bytes() == b"PNGDATA"
    fid = re.search(r'/files/(\d+)/download', r.text).group(1)
    assert c.get(f"/files/{fid}/download").content == b"PNGDATA"

    # admin widzi plik i komentarz, odpowiada plikiem
    page = admin.get("/admin/clients/2").text
    assert "passwd logo.png" in page and "wersja 2" in page
    tok = csrf_of(admin, "/admin/clients/2")
    admin.post("/admin/clients/2/upload", data={"csrf": tok, "note": ""},
               files=[("files", ("projekt.pdf", b"PDF", "application/pdf"))])
    assert (app.state.settings.clients_dir / "jan.kowalski" / "od BSK Media" / "projekt.pdf").exists()
    page = c.get("/files").text
    assert "projekt.pdf" in page

    # klient nie usunie pliku od admina ani nie zobaczy panelu admina
    admin_fid = re.findall(r'/files/(\d+)/download', page)[0]
    tok = csrf_of(c, "/files")
    assert c.post(f"/files/{admin_fid}/delete", data={"csrf": tok}).status_code == 403
    assert c.get("/admin").status_code == 403

    # blokada konta wylogowuje klienta
    tok = csrf_of(admin, "/admin/clients/2")
    admin.post("/admin/clients/2/toggle", data={"csrf": tok})
    assert c.get("/files", follow_redirects=False).status_code == 303


def test_other_client_cannot_download(app, admin):
    create_client_account(admin, "a.klient", "KlientHaslo1")
    create_client_account(admin, "b.klient", "KlientHaslo2")
    tok = csrf_of(admin, "/admin/clients/2")
    admin.post("/admin/clients/2/upload", data={"csrf": tok}, files=[("files", ("tajne.txt", b"x"))])
    b = TestClient(app)
    login(b, "b.klient", "KlientHaslo2")
    tok = csrf_of(b, "/account")
    b.post("/account/password", data={"current": "KlientHaslo2", "new": "NoweHaslo123", "repeat": "NoweHaslo123", "csrf": tok})
    r = b.get("/files/1/download")
    assert r.status_code == 404 and r.content != b"x"


def test_upload_too_large(app, admin):
    create_client_account(admin)
    tok = csrf_of(admin, "/admin/clients/2")
    r = admin.post("/admin/clients/2/upload", data={"csrf": tok},
                   files=[("files", ("duzy.bin", b"0" * (3 * 1024 * 1024)))])
    assert r.status_code == 413


def make_device(admin):
    tok = csrf_of(admin, "/admin/devices/new")
    r = admin.post("/admin/devices/new", data={"name": "LAPTOP-ANNA", "employee": "Anna", "csrf": tok})
    return re.search(r'class="token">([^<]+)<', r.text).group(1)


def test_agent_backup_and_versions(app, admin):
    token = make_device(admin)
    h = {"Authorization": f"Bearer {token}"}
    api = TestClient(app)
    assert api.get("/api/agent/ping", headers=h).json()["device"] == "LAPTOP-ANNA"
    assert api.get("/api/agent/ping", headers={"Authorization": "Bearer zly"}).status_code == 401

    base = app.state.settings.backups_dir / "laptop-anna"
    for i in range(4):
        data = f"wersja {i}".encode()
        sha = hashlib.sha256(data).hexdigest()
        assert api.get("/api/agent/status", params={"path": "Dokumenty/oferta.docx", "sha256": sha}, headers=h).json()["needed"]
        r = api.put("/api/agent/files", params={"path": "Dokumenty/oferta.docx", "sha256": sha, "mtime": 1700000000 + i},
                    content=data, headers=h)
        assert r.json()["status"] == "stored", r.text
        import time; time.sleep(1.05)  # różne znaczniki wersji
    assert (base / "Dokumenty" / "oferta.docx").read_bytes() == b"wersja 3"
    versions = list((base / ".wersje" / "Dokumenty").iterdir())
    assert len(versions) == 2  # backup_keep_versions=2

    # bez zmian -> unchanged
    sha = hashlib.sha256(b"wersja 3").hexdigest()
    r = api.put("/api/agent/files", params={"path": "Dokumenty/oferta.docx", "sha256": sha}, content=b"wersja 3", headers=h)
    assert r.json()["status"] == "unchanged"

    # zła suma kontrolna i próba wyjścia poza katalog
    r = api.put("/api/agent/files", params={"path": "a.txt", "sha256": "0" * 64}, content=b"abc", headers=h)
    assert r.status_code == 409
    r = api.put("/api/agent/files", params={"path": "../../x.txt", "sha256": hashlib.sha256(b"a").hexdigest()},
                content=b"a", headers=h)
    assert r.status_code == 400

    page = admin.get("/admin/devices/1").text
    assert "Dokumenty/oferta.docx" in page
    v = admin.get("/admin/devices/1/versions", params={"path": "Dokumenty/oferta.docx"}).text
    vname = re.search(r'version=([^"&]+)', v).group(1)
    from urllib.parse import unquote
    r = admin.get("/admin/devices/1/download", params={"path": "Dokumenty/oferta.docx", "version": unquote(vname)})
    assert r.content in (b"wersja 1", b"wersja 2")


def test_safe_paths():
    assert storage.safe_name("..\\..\\CON.txt") == "_CON.txt"
    assert storage.safe_name("..") == "plik"
    assert storage.safe_name(".gitignore") == ".gitignore"
    assert storage.safe_rel_path("C:/Users/x") == "C_/Users/x"
    with pytest.raises(ValueError):
        storage.safe_rel_path("a/../../b")
    assert storage.safe_rel_path(".wersje/a") == "_.wersje/a"


def change_pw(c, old, new="NoweHaslo123"):
    tok = csrf_of(c, "/account")
    c.post("/account/password", data={"current": old, "new": new, "repeat": new, "csrf": tok})


def test_client_quota(app, admin):
    tok = csrf_of(admin, "/admin/clients/new")
    admin.post("/admin/clients/new", data={"username": "maly", "password": "KlientHaslo1", "quota_mb": 1, "csrf": tok})
    c = TestClient(app)
    login(c, "maly", "KlientHaslo1")
    change_pw(c, "KlientHaslo1")
    tok = csrf_of(c, "/files")
    r = c.post("/files/upload", data={"csrf": tok}, files=[("files", ("a.bin", b"0" * 700_000))])
    assert "Wysłano plików: 1" in r.text and "Wykorzystane miejsce" in r.text
    r = c.post("/files/upload", data={"csrf": tok}, files=[("files", ("b.bin", b"0" * 700_000))])
    assert r.status_code == 413 and "Brak miejsca" in r.text
    assert not (app.state.settings.clients_dir / "maly" / "b.bin").exists()
    # pliki od admina nie zabierają klientowi miejsca
    tok = csrf_of(admin, "/admin/clients/2")
    admin.post("/admin/clients/2/upload", data={"csrf": tok}, files=[("files", ("od-nas.bin", b"0" * 900_000))])
    tok = csrf_of(c, "/files")
    r = c.post("/files/upload", data={"csrf": tok}, files=[("files", ("c.bin", b"0" * 100_000))])
    assert r.status_code == 200
    # admin zmienia limit na 0 = bez limitu
    tok = csrf_of(admin, "/admin/clients/2")
    admin.post("/admin/clients/2/edit", data={"display_name": "", "company": "", "email": "", "quota_mb": 0, "csrf": tok})
    tok = csrf_of(c, "/files")
    r = c.post("/files/upload", data={"csrf": tok}, files=[("files", ("d.bin", b"0" * 700_000))])
    assert r.status_code == 200


def test_share_link_upload_password_and_range(app, admin):
    tok = csrf_of(admin, "/admin/shares")
    r = admin.post("/admin/shares/upload", data={"csrf": tok, "expires_days": 7, "max_downloads": 2, "password": "tajne"},
                   files=[("files", ("grafika.tif", b"0123456789" * 1000))])
    path = "/s/" + re.search(r'/s/([A-Za-z0-9_-]+)"', r.text).group(1)
    anon = TestClient(app)
    page = anon.get(path)
    assert "grafika.tif" in page.text and "Hasło do pliku" in page.text
    assert anon.get(path + "/pobierz", follow_redirects=False).status_code == 303  # bez hasła
    tok = csrf_of(anon, path)
    anon.post(path, data={"password": "zle", "csrf": tok})
    anon.post(path, data={"password": "tajne", "csrf": tok})
    r = anon.get(path + "/pobierz")
    assert r.status_code == 200 and len(r.content) == 10000
    # wznowienie pobierania (Range) nie zużywa limitu
    r = anon.get(path + "/pobierz", headers={"Range": "bytes=5000-"})
    assert r.status_code == 206 and len(r.content) == 5000
    assert anon.get(path + "/pobierz").status_code == 200
    assert anon.get(path).status_code == 404  # limit 2 pobrań wykorzystany


def test_share_existing_nas_file_and_revoke(app, admin):
    shared = app.state.settings.shared_dir / "Kampania"
    shared.mkdir(parents=True)
    (shared / "film.mp4").write_bytes(b"MP4")
    page = admin.get("/admin/shares").text
    assert "Kampania/film.mp4" in page
    tok = csrf_of(admin, "/admin/shares")
    r = admin.post("/admin/shares/new", data={"csrf": tok, "kind": "shared", "rel_path": "Kampania/film.mp4", "expires_days": 0})
    token = re.search(r'/s/([A-Za-z0-9_-]+)"', r.text).group(1)
    anon = TestClient(app)
    assert anon.get(f"/s/{token}/pobierz").content == b"MP4"
    r = admin.post("/admin/shares/new", data={"csrf": tok, "kind": "shared", "rel_path": "../app/bskcloud.db"})
    assert r.status_code in (400, 404)
    tok = csrf_of(admin, "/admin/shares")
    admin.post("/admin/shares/1/revoke", data={"csrf": tok})
    assert anon.get(f"/s/{token}/pobierz").status_code == 404


def test_share_client_file(app, admin):
    create_client_account(admin)
    tok = csrf_of(admin, "/admin/clients/2")
    admin.post("/admin/clients/2/upload", data={"csrf": tok}, files=[("files", ("logo.ai", b"AI"))])
    tok = csrf_of(admin, "/admin/clients/2")
    r = admin.post("/admin/shares/new", data={"csrf": tok, "kind": "file", "file_id": 1, "expires_days": 7,
                                              "back": "/admin/clients/2"})
    assert "Link do pobrania gotowy" in r.text
    token = re.search(r'/s/([A-Za-z0-9_-]+)"', r.text).group(1)
    assert TestClient(app).get(f"/s/{token}/pobierz").content == b"AI"


def test_install_link_and_device_quota(app, admin):
    import io
    import zipfile
    tok = csrf_of(admin, "/admin/devices/new")
    r = admin.post("/admin/devices/new", data={"name": "PC-1", "employee": "Ola", "quota_mb": 1, "csrf": tok})
    code = re.search(r'/instaluj/([A-Za-z0-9_-]+)"', r.text).group(1)
    token = re.search(r'class="token">([^<]+)<', r.text).group(1)
    anon = TestClient(app)
    assert "nie jest jeszcze dostępny" in anon.get(f"/instaluj/{code}").text
    for f in ("bsk-backup.exe", "bsk-backup-agent.exe", "install-windows.ps1", "uninstall-windows.ps1"):
        (app.state.settings.installer_dir / f).write_bytes(b"x")
    assert "ZAINSTALUJ.cmd" in anon.get(f"/instaluj/{code}").text
    z = zipfile.ZipFile(io.BytesIO(anon.get(f"/instaluj/{code}/BSK-Backup.zip").content))
    cmd = z.read("BSK-Backup/ZAINSTALUJ.cmd").decode()
    assert token in cmd and "install-windows.ps1" in cmd
    assert not list(app.state.settings.tmp_dir.glob("installer-*"))  # plik tymczasowy sprzątnięty

    h = {"Authorization": f"Bearer {token}"}
    big = b"0" * 800_000
    r = anon.put("/api/agent/files", params={"path": "a/1.bin", "sha256": hashlib.sha256(big).hexdigest()}, content=big, headers=h)
    assert r.status_code == 200
    r = anon.put("/api/agent/files", params={"path": "a/2.bin", "sha256": hashlib.sha256(big).hexdigest()}, content=big, headers=h)
    assert r.status_code == 507
    # nowy link instalacyjny unieważnia stary token
    tok = csrf_of(admin, "/admin/devices/1")
    admin.post("/admin/devices/1/install-link", data={"csrf": tok})
    assert anon.get("/api/agent/ping", headers=h).status_code == 401
    assert anon.get(f"/instaluj/{code}").status_code == 404


def test_db_on_separate_disk(tmp_path):
    s = Settings(data_dir=tmp_path / "hdd", db_dir=tmp_path / "ssd", secret_key="x" * 40, cookie_secure=False,
                 admin_username="admin", admin_password=ADMIN_PW)
    create_app(s)
    assert (tmp_path / "ssd" / "bskcloud.db").exists()
    assert not (tmp_path / "hdd" / "app" / "bskcloud.db").exists()
    assert (tmp_path / "hdd" / "klienci").is_dir()


def test_browser_chunked_upload_resume_and_quota(app, admin, monkeypatch):
    from app import chunks
    create_client_account(admin)
    c = TestClient(app)
    login(c, "jan.kowalski", "KlientHaslo1")
    change_pw(c, "KlientHaslo1")
    tok = csrf_of(c, "/files")
    h = {"X-CSRF-Token": tok}
    data = bytes(range(256)) * 4000  # ~1 MB
    assert c.post("/api/uploads", json={"name": "film.mp4", "size": len(data)}).status_code == 400  # bez CSRF
    up = c.post("/api/uploads", json={"name": "film.mp4", "size": len(data)}, headers=h).json()
    uid = up["id"]
    r = c.put(f"/api/uploads/{uid}", params={"offset": 0}, content=data[:300_000], headers=h)
    assert r.json()["received"] == 300_000
    # zły offset (np. po zerwanym połączeniu) -> serwer mówi, skąd wznowić
    r = c.put(f"/api/uploads/{uid}", params={"offset": 999}, content=b"x", headers=h)
    assert r.status_code == 409 and r.json()["received"] == 300_000
    r = c.put(f"/api/uploads/{uid}", params={"offset": 300_000}, content=data[300_000:], headers=h)
    assert r.json()["status"] == "done"
    # cudza wysyłka nie do odebrania
    other = TestClient(app)
    login(other, "admin", ADMIN_PW)
    t2 = csrf_of(other, "/admin/clients/2")
    assert other.post("/admin/clients/2/upload", data={"csrf": t2, "upload_ids": uid}).status_code == 400
    # odbiór przez formularz klienta
    r = c.post("/files/upload", data={"csrf": tok, "note": "duży plik", "upload_ids": uid})
    assert "Wysłano plików: 1" in r.text
    stored = app.state.settings.clients_dir / "jan.kowalski" / "film.mp4"
    assert stored.read_bytes() == data
    assert not list((app.state.settings.tmp_dir / "chunks").glob("*.part"))
    row = app.state.db.one("SELECT sha256 FROM files WHERE original_name = 'film.mp4'")
    assert row["sha256"] == hashlib.sha256(data).hexdigest()
    # limit miejsca sprawdzany już przy rozpoczęciu wysyłki
    t3 = csrf_of(admin, "/admin/clients/2")
    admin.post("/admin/clients/2/edit", data={"display_name": "", "company": "", "email": "", "quota_mb": 1, "csrf": t3})
    r = c.post("/api/uploads", json={"name": "b.bin", "size": 200_000}, headers=h)
    assert r.status_code == 413 and "Brak miejsca" in r.json()["detail"]


def test_chunk_hash_rebuilt_after_restart(app, admin, tmp_path):
    from app import chunks
    store = chunks.ChunkStore(app.state.db, app.state.settings.tmp_dir)
    data = b"abc" * 100_000
    row = store.create(name="x", size=len(data), user_id=1)

    async def stream(b):
        yield b

    import asyncio
    asyncio.run(store.write(row, 0, stream(data[:100_000])))
    fresh = chunks.ChunkStore(app.state.db, app.state.settings.tmp_dir)  # „restart”: brak stanu w pamięci
    done = asyncio.run(fresh.write(fresh.get(row["id"]), 100_000, stream(data[100_000:])))
    assert done["status"] == "done" and done["sha256"] == hashlib.sha256(data).hexdigest()


def test_admin_share_upload_chunked(app, admin):
    tok = csrf_of(admin, "/admin/shares")
    h = {"X-CSRF-Token": tok}
    up = admin.post("/api/uploads", json={"name": "billboard.tif", "size": 5}, headers=h).json()
    admin.put(f"/api/uploads/{up['id']}", params={"offset": 0}, content=b"12345", headers=h)
    r = admin.post("/admin/shares/upload", data={"csrf": tok, "expires_days": 7, "upload_ids": up["id"]})
    assert "Link do pobrania gotowy" in r.text
    assert (app.state.settings.shared_dir / "billboard.tif").read_bytes() == b"12345"


def test_agent_chunked_upload(app, admin):
    token = make_device(admin)
    h = {"Authorization": f"Bearer {token}"}
    api = TestClient(app)
    data = os.urandom(250_000)
    sha = hashlib.sha256(data).hexdigest()
    body = {"path": "Wideo/klip.mov", "size": len(data), "sha256": sha, "mtime": 1700000000}
    init = api.post("/api/agent/uploads", json=body, headers=h).json()
    assert init["status"] == "upload" and init["received"] == 0
    api.put(f"/api/agent/uploads/{init['id']}", params={"offset": 0}, content=data[:100_000], headers=h)
    # wznowienie: ten sam plik -> ta sama wysyłka, od miejsca przerwania
    again = api.post("/api/agent/uploads", json=body, headers=h).json()
    assert again["id"] == init["id"] and again["received"] == 100_000
    r = api.put(f"/api/agent/uploads/{init['id']}", params={"offset": 100_000}, content=data[100_000:], headers=h)
    assert r.json()["status"] == "stored", r.text
    assert (app.state.settings.backups_dir / "laptop-anna" / "Wideo" / "klip.mov").read_bytes() == data
    assert api.post("/api/agent/uploads", json=body, headers=h).json()["status"] == "unchanged"
    # plik zmienił się w trakcie -> 409, nic nie zapisane
    bad = dict(body, path="Wideo/inny.mov", sha256="0" * 64)
    b = api.post("/api/agent/uploads", json=bad, headers=h).json()
    r = api.put(f"/api/agent/uploads/{b['id']}", params={"offset": 0}, content=data, headers=h)
    assert r.status_code == 409
    assert not (app.state.settings.backups_dir / "laptop-anna" / "Wideo" / "inny.mov").exists()
    # cudze urządzenie nie dopisze do wysyłki
    assert api.put(f"/api/agent/uploads/{b['id']}", params={"offset": 0}, content=b"x",
                   headers={"Authorization": "Bearer zly"}).status_code == 401


def test_backup_to_second_disk(tmp_path):
    import json
    import sqlite3
    from app import backup
    s = Settings(data_dir=tmp_path / "hdd", db_dir=tmp_path / "ssd", backup_dir=tmp_path / "ssd2",
                 secret_key="x" * 40, cookie_secure=False, admin_username="admin", admin_password=ADMIN_PW)
    app = create_app(s)
    (s.clients_dir / "jan").mkdir(parents=True)
    (s.clients_dir / "jan" / "logo.ai").write_bytes(b"AI")
    (s.shared_dir / "film.mp4").write_bytes(b"MP4")
    (s.backups_dir / "pc" ).mkdir(parents=True)
    (s.backups_dir / "pc" / "duzy.bin").write_bytes(b"x")
    r = backup.run_backup(s, app.state.db)
    assert r["copied_files"] == 2
    assert (s.backup_dir / "klienci" / "jan" / "logo.ai").read_bytes() == b"AI"
    assert (s.backup_dir / "udostepnione" / "film.mp4").exists()
    assert not (s.backup_dir / "kopie-pracownikow").exists()
    snap = next((s.backup_dir / "baza").glob("bskcloud-*.db"))
    assert sqlite3.connect(snap).execute("SELECT username FROM users").fetchone()[0] == "admin"
    # druga kopia: nic nowego; usunięty plik zostaje w kopii
    (s.clients_dir / "jan" / "logo.ai").unlink()
    assert backup.run_backup(s, app.state.db)["copied_files"] == 0
    assert (s.backup_dir / "klienci" / "jan" / "logo.ai").exists()
    assert json.loads((s.backup_dir / "ostatnia-kopia.json").read_text())["error"] == ""
    # panel admina pokazuje stan i pozwala uruchomić kopię
    c = TestClient(app)
    login(c, "admin", ADMIN_PW)
    page = c.get("/admin").text
    assert "Kopia na drugi dysk" in page and "Ostatnia kopia" in page
    tok = csrf_of(c, "/admin")
    assert "Kopia uruchomiona" in c.post("/admin/backup/run", data={"csrf": tok}).text


@pytest.fixture()
def office_app(tmp_path):
    s = Settings(data_dir=tmp_path, secret_key="x" * 40, cookie_secure=False, admin_username="admin",
                 admin_password=ADMIN_PW, office_url="https://cloud.example/office", office_secret="s" * 32,
                 app_internal_url="http://app:8000", public_url="https://cloud.example")
    return create_app(s)


def test_office_edit_and_save(office_app):
    import http.server
    import threading
    import urllib.parse
    from app import office
    app = office_app
    admin = TestClient(app)
    login(admin, "admin", ADMIN_PW)
    create_client_account(admin)
    tok = csrf_of(admin, "/admin/clients/2")
    r = admin.post("/admin/clients/2/new-doc", data={"csrf": tok, "kind": "xlsx", "title": "Budżet"},
                   follow_redirects=False)
    assert r.headers["location"] == "/files/1/edit"
    page = admin.get("/files/1/edit")
    assert page.status_code == 200 and "https://cloud.example/office/web-apps/apps/api/documents/api.js" in page.text
    csp = page.headers["content-security-policy"]
    assert "frame-src https://cloud.example/office/;" in csp and "script-src 'self' https://cloud.example/office/;" in csp
    cfg = json.loads(html.unescape(re.search(r'data-config="([^"]+)"', page.text).group(1)))
    assert cfg["document"]["key"] == "bsk-1-1" and cfg["editorConfig"]["mode"] == "edit"
    assert office.jwt_decode(cfg["token"], "s" * 32)["document"]["title"] == "Budżet.xlsx"
    # serwer dokumentów pobiera plik po podpisanym adresie (bez sesji)
    file_url = urllib.parse.urlsplit(cfg["document"]["url"])
    anon = TestClient(app)
    assert anon.get(file_url.path + "?" + file_url.query).content[:2] == b"PK"
    assert anon.get(file_url.path + "?sig=1.zly").status_code == 403
    # klient też może edytować ten sam plik (współedycja)
    c = TestClient(app)
    login(c, "jan.kowalski", "KlientHaslo1")
    change_pw(c, "KlientHaslo1")
    ccfg = json.loads(html.unescape(re.search(r'data-config="([^"]+)"',
                                               c.get("/files/1/edit").text).group(1)))
    assert ccfg["document"]["key"] == cfg["document"]["key"]  # ta sama sesja edycji
    # callback z nową wersją: serwer dokumentów udostępnia plik pod adresem url
    new_content = b"PK-nowa-wersja"

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(new_content)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cb = urllib.parse.urlsplit(cfg["editorConfig"]["callbackUrl"])
    payload = {"key": "bsk-1-1", "status": 2, "url": f"http://127.0.0.1:{srv.server_port}/cache/x.xlsx", "users": ["2"]}
    bad = anon.post(cb.path + "?" + cb.query, json={**payload, "token": office.jwt_encode(payload, "zly" * 11)})
    assert bad.json()["error"] == 1
    r = anon.post(cb.path + "?" + cb.query, json={**payload, "token": office.jwt_encode(payload, "s" * 32)})
    srv.shutdown()
    assert r.json() == {"error": 0}
    stored = app.state.settings.clients_dir / "jan.kowalski" / "od BSK Media" / "Budżet.xlsx"
    assert stored.read_bytes() == new_content
    assert list((stored.parent / ".wersje").iterdir())  # poprzednia wersja zachowana
    row = app.state.db.one("SELECT * FROM files WHERE id = 1")
    assert row["office_version"] == 2 and row["modified_by"] == 2 and row["size"] == len(new_content)
    assert "edytowano" in c.get("/files").text


def test_blank_documents_are_valid():
    import io
    import zipfile
    from xml.dom import minidom
    from app import office
    for kind in ("xlsx", "docx"):
        data, ext = office.blank_document(kind)
        z = zipfile.ZipFile(io.BytesIO(data))
        for name in z.namelist():
            minidom.parseString(z.read(name))  # każda część to poprawny XML
        assert "[Content_Types].xml" in z.namelist()


def test_office_disabled_and_jwt(app, admin):
    from app import office
    create_client_account(admin)
    tok = csrf_of(admin, "/admin/clients/2")
    r = admin.post("/admin/clients/2/new-doc", data={"csrf": tok, "kind": "docx", "title": ""}, follow_redirects=False)
    assert r.headers["location"] == "/admin/clients/2"  # bez serwera dokumentów: plik do pobrania
    assert admin.get("/files/1/edit").status_code == 404
    assert "Edytuj online" not in admin.get("/admin/clients/2").text
    t = office.jwt_encode({"a": 1, "exp": 1}, "k")
    with pytest.raises(ValueError):
        office.jwt_decode(t, "k")  # wygasły


def test_quota_in_gb_and_device_folders(app, admin):
    tok = csrf_of(admin, "/admin/devices/new")
    r = admin.post("/admin/devices/new", data={"name": "LAPTOP-OLAF", "employee": "Olaf", "quota_gb": "1024",
                                               "special": ["@pulpit", "@dokumenty", "@zly"],
                                               "extra_folders": "C:\\Projekty\n\n D:\\Klienci ", "csrf": tok})
    token = re.search(r'class="token">([^<]+)<', r.text).group(1)
    d = app.state.db.one("SELECT * FROM devices WHERE name = 'LAPTOP-OLAF'")
    assert d["quota_mb"] == 1024 * 1024
    assert d["folders"].splitlines() == ["@pulpit", "@dokumenty", "C:\\Projekty", "D:\\Klienci"]
    cfg = TestClient(app).get("/api/agent/config", headers={"Authorization": f"Bearer {token}"}).json()
    assert cfg["folders"] == ["@pulpit", "@dokumenty", "C:\\Projekty", "D:\\Klienci"]
    page = admin.get("/admin/devices/1").text
    assert "1 TB" in page and 'value="1024"' in page
    assert "Olaf" in admin.get("/admin/devices").text
    tok = csrf_of(admin, "/admin/clients/new")
    admin.post("/admin/clients/new", data={"username": "firma", "password": "KlientHaslo1", "quota_gb": "2,5", "csrf": tok})
    assert app.state.db.one("SELECT quota_mb FROM users WHERE username = 'firma'")["quota_mb"] == 2560


def test_dashboard_and_lists_render(app, admin):
    create_client_account(admin)
    tok = csrf_of(admin, "/admin/clients/2")
    admin.post("/admin/clients/2/upload", data={"csrf": tok}, files=[("files", ("logo.png", b"x" * 1000))])
    page = admin.get("/admin").text
    for text in ("Aktywni klienci", "Przesłane pliki", "Dyski NAS", "Najwięcej miejsca", "logo.png", "<svg"):
        assert text in page, text
    assert "ACME" in admin.get("/admin/clients").text
    assert "ACME" in admin.get("/admin/clients?q=acme").text
    assert "Brak klientów" in admin.get("/admin/clients?q=zzz").text
    assert 'style="' not in page  # CSP: bez stylów inline


def test_static_assets_versioned(app):
    page = TestClient(app).get("/login").text
    m = re.search(r'/static/style\.css\?v=([0-9a-f]{10})"', page)
    assert m and re.search(r'/static/app\.js\?v=' + m.group(1), page)


@pytest.fixture()
def mail_app(tmp_path, monkeypatch):
    from app import notify
    sent = []
    monkeypatch.setattr(notify, "_deliver", lambda settings, to, subject, text, html: sent.append((to, subject, text, html)))
    notify._last_sent.clear()
    s = Settings(data_dir=tmp_path, secret_key="x" * 40, cookie_secure=False, admin_username="admin",
                 admin_password=ADMIN_PW, smtp_host="smtp.hostinger.com", smtp_port=465, smtp_user="cloud@bskmedia.pl",
                 smtp_password="x", smtp_from="cloud@bskmedia.pl", notify_email="biuro@bskmedia.pl",
                 public_url="https://cloud.bskmedia.pl")
    app = create_app(s)
    app.state.sent = sent
    return app


def wait_for_mail(sent, n):
    import time
    for _ in range(50):
        if len(sent) >= n:
            return True
        time.sleep(0.05)
    return False


def test_unread_live_thread_and_emails(mail_app):
    app, sent = mail_app, mail_app.state.sent
    admin = TestClient(app)
    login(admin, "admin", ADMIN_PW)
    tok = csrf_of(admin, "/admin/clients/new")
    admin.post("/admin/clients/new", data={"username": "acme", "company": "ACME", "email": "anna@acme.pl",
                                           "password": "KlientHaslo1", "csrf": tok})
    c = TestClient(app)
    login(c, "acme", "KlientHaslo1")
    change_pw(c, "KlientHaslo1")
    ctok = csrf_of(c, "/files")
    # klient pisze -> admin widzi licznik i dostaje e-mail
    c.post("/files/message", data={"body": "Dzień dobry, kiedy projekt?", "csrf": ctok})
    c.post("/files/message", data={"body": "Druga wiadomość", "csrf": ctok})
    assert admin.get("/api/unread").json()["unread"] == 2
    assert 'data-unread-badge >2<' in admin.get("/admin").text.replace("\n", "") or ">2</span>" in admin.get("/admin").text
    assert "2 nowe" in admin.get("/admin/clients").text
    assert wait_for_mail(sent, 1)
    to, subject, text, html_body = sent[0]
    assert to == "biuro@bskmedia.pl" and "ACME" in subject and "kiedy projekt" in text and "/admin/clients/2" in html_body
    import time; time.sleep(0.2)
    assert len(sent) == 1  # druga wiadomość w ciągu 5 minut nie wysyła kolejnego maila
    # admin otwiera rozmowę -> licznik znika
    admin.get("/admin/clients/2")
    assert admin.get("/api/unread").json()["unread"] == 0
    # API rozmowy: nowe wiadomości po id
    thread = admin.get("/api/thread/2", params={"after": 0}).json()["messages"]
    assert [m["body"] for m in thread] == ["Dzień dobry, kiedy projekt?", "Druga wiadomość"] and not thread[0]["mine"]
    last = thread[-1]["id"]
    # admin odpowiada (jak z JS) -> klient: licznik + e-mail do klienta
    atok = csrf_of(admin, "/admin/clients/2")
    r = admin.post("/admin/clients/2/message", data={"body": "Projekt będzie w piątek.", "csrf": atok},
                   headers={"X-Requested-With": "fetch"})
    assert r.json()["redirect"].startswith("/admin/clients/2")
    assert [m["body"] for m in admin.get("/api/thread/2", params={"after": last}).json()["messages"]] == ["Projekt będzie w piątek."]
    assert c.get("/api/unread").json()["unread"] == 1
    assert wait_for_mail(sent, 2) and sent[1][0] == "anna@acme.pl" and "piątek" in sent[1][2]
    page = c.get("/files").text
    assert "Masz <b>1</b> nowość" in page
    assert c.get("/api/unread").json()["unread"] == 0
    # klient nie podejrzy cudzej rozmowy
    assert c.get("/api/thread/1").status_code == 403
    # wyłączenie powiadomień klienta
    atok = csrf_of(admin, "/admin/clients/2")
    admin.post("/admin/clients/2/edit", data={"company": "ACME", "display_name": "", "email": "anna@acme.pl",
                                              "quota_gb": "0", "csrf": atok})
    from app import notify
    notify._last_sent.clear()
    admin.post("/admin/clients/2/message", data={"body": "Bez maila", "csrf": atok})
    time.sleep(0.2)
    assert len(sent) == 2


def test_email_test_button(mail_app):
    admin = TestClient(mail_app)
    login(admin, "admin", ADMIN_PW)
    page = admin.get("/account").text
    assert "smtp.hostinger.com" in page and "biuro@bskmedia.pl" in page
    tok = csrf_of(admin, "/account")
    r = admin.post("/admin/test-email", data={"csrf": tok})
    assert "Wysłano e-mail testowy na biuro@bskmedia.pl" in r.text
    assert mail_app.state.sent[-1][1] == "[BSK Media Cloud] Test powiadomień"


def test_email_not_configured(app, admin):
    tok = csrf_of(admin, "/account")
    assert "Poczta nie jest skonfigurowana" in admin.post("/admin/test-email", data={"csrf": tok}).text


def _png(w=800, h=600, color=(42, 120, 214)):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_folders_versions_move_and_trash(app, admin):
    create_client_account(admin)
    s = app.state.settings
    tok = csrf_of(admin, "/admin/clients/2")
    r = admin.post("/spaces/2/folders", data={"csrf": tok, "name": "Kampania jesień"})
    assert "Utworzono katalog" in r.text
    fid = app.state.db.one("SELECT id FROM folders WHERE name = 'Kampania jesień'")["id"]
    assert "już istnieje" in admin.post("/spaces/2/folders", data={"csrf": tok, "name": "kampania JESIEŃ"}).text
    admin.post("/spaces/2/folders", data={"csrf": tok, "name": "Zdjęcia", "parent_id": fid})
    sub = app.state.db.one("SELECT id FROM folders WHERE name = 'Zdjęcia'")["id"]
    # wysyłka do podkatalogu -> plik w odpowiednim miejscu na NAS
    admin.post("/admin/clients/2/upload", data={"csrf": tok, "folder_id": sub}, files=[("files", ("plakat.png", _png()))])
    path = s.clients_dir / "jan.kowalski" / "Kampania jesień" / "Zdjęcia" / "plakat.png"
    assert path.exists()
    page = admin.get(f"/admin/clients/2?folder={sub}").text
    assert "plakat.png" in page and "Kampania jesień" in page and 'class="gcard"' in page
    # ta sama nazwa w tym samym katalogu -> nowa wersja (to samo id, stara w .wersje)
    admin.post("/admin/clients/2/upload", data={"csrf": tok, "folder_id": sub},
               files=[("files", ("plakat.png", _png(color=(235, 104, 52))))])
    rows = app.state.db.all("SELECT * FROM files WHERE original_name = 'plakat.png'")
    assert len(rows) == 1 and rows[0]["version"] == 2
    assert app.state.db.one("SELECT COUNT(*) AS n FROM file_versions")["n"] == 1
    assert list((path.parent / ".wersje").iterdir())
    assert "v2" in admin.get(f"/admin/clients/2?folder={sub}").text
    file_id = rows[0]["id"]
    # zmiana nazwy katalogu nadrzędnego przenosi pliki na dysku i w bazie
    admin.post(f"/folders/{fid}/rename", data={"csrf": tok, "name": "Kampania 2026"})
    moved = s.clients_dir / "jan.kowalski" / "Kampania 2026" / "Zdjęcia" / "plakat.png"
    assert moved.exists() and not path.exists()
    assert admin.get(f"/files/{file_id}/download").status_code == 200
    # przeniesienie do katalogu głównego
    admin.post(f"/files/{file_id}/move", data={"csrf": tok, "folder_id": ""})
    f = app.state.db.one("SELECT * FROM files WHERE id = ?", file_id)
    assert f["folder_id"] is None and (s.clients_dir / f["rel_path"]).exists()
    # kosz: usunięcie, niewidoczne dla klienta, przywrócenie
    admin.post(f"/files/{file_id}/delete", data={"csrf": tok})
    assert admin.get(f"/files/{file_id}/download").status_code == 404
    trash = admin.get("/admin/trash").text
    assert "plakat.png" in trash and "30 dni" in trash
    admin.post(f"/admin/trash/{file_id}/restore", data={"csrf": csrf_of(admin, "/admin/trash")})
    assert admin.get(f"/files/{file_id}/download").status_code == 200
    # usunięcie katalogu -> pliki do kosza, opróżnienie kosza usuwa na zawsze
    admin.post(f"/files/{file_id}/move", data={"csrf": tok, "folder_id": sub})
    admin.post(f"/folders/{fid}/delete", data={"csrf": tok})
    assert app.state.db.one("SELECT COUNT(*) AS n FROM folders")["n"] == 0
    assert app.state.db.one("SELECT deleted_at FROM files WHERE id = ?", file_id)["deleted_at"]
    admin.post("/admin/trash/empty", data={"csrf": csrf_of(admin, "/admin/trash")})
    assert app.state.db.one("SELECT COUNT(*) AS n FROM files")["n"] == 0
    assert not any(s.trash_dir.iterdir())


def test_trash_auto_purge_after_30_days(app, admin):
    create_client_account(admin)
    tok = csrf_of(admin, "/admin/clients/2")
    admin.post("/admin/clients/2/upload", data={"csrf": tok}, files=[("files", ("stary.txt", b"x"))])
    admin.post("/files/1/delete", data={"csrf": tok})
    app.state.db.run("UPDATE files SET deleted_at = '2020-01-01T00:00:00+00:00' WHERE id = 1")
    admin.get("/admin/trash")
    assert app.state.db.one("SELECT COUNT(*) AS n FROM files")["n"] == 0


def test_my_drive_is_private(app, admin):
    create_client_account(admin)
    tok = csrf_of(admin, "/admin/my")
    admin.post("/spaces/1/folders", data={"csrf": tok, "name": "Prywatne"})
    folder = app.state.db.one("SELECT id FROM folders WHERE owner_id = 1")["id"]
    admin.post("/admin/my/upload", data={"csrf": tok, "folder_id": folder}, files=[("files", ("paszport.jpg", _png()))])
    s = app.state.settings
    assert (s.clients_dir / "_prywatne" / "admin" / "Prywatne" / "paszport.jpg").exists()
    page = admin.get(f"/admin/my?folder={folder}").text
    assert "paszport.jpg" in page and "Mój dysk" in page
    # nie pojawia się w statystykach klientów ani na pulpicie
    assert "paszport.jpg" not in admin.get("/admin").text
    c = TestClient(app)
    login(c, "jan.kowalski", "KlientHaslo1")
    change_pw(c, "KlientHaslo1")
    assert c.get("/files/1/download").status_code == 404
    assert c.get("/files/1/thumb").status_code == 404
    assert "paszport.jpg" not in c.get("/search", params={"q": "paszport"}).text
    assert c.post("/spaces/1/folders", data={"csrf": csrf_of(c, "/files"), "name": "x"}).status_code == 403
    assert "paszport.jpg" in admin.get("/search", params={"q": "paszp"}).text


def test_thumbnails_and_safe_preview(app, admin):
    import shutil as sh
    import subprocess
    create_client_account(admin)
    tok = csrf_of(admin, "/admin/clients/2")
    files = [("files", ("zdjecie.png", _png(3000, 2000))), ("files", ("strona.html", b"<script>alert(1)</script>")),
             ("files", ("grafika.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"))]
    if sh.which("pdftoppm"):
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (600, 800), (255, 255, 255)).save(buf, "PDF")
        files.append(("files", ("oferta.pdf", buf.getvalue())))
    admin.post("/admin/clients/2/upload", data={"csrf": tok}, files=files)
    ids = {r["original_name"]: r["id"] for r in app.state.db.all("SELECT id, original_name FROM files")}
    r = admin.get(f"/files/{ids['zdjecie.png']}/thumb")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    from PIL import Image
    import io
    assert max(Image.open(io.BytesIO(r.content)).size) == 520
    big = admin.get(f"/files/{ids['zdjecie.png']}/thumb", params={"s": "preview"})
    assert max(Image.open(io.BytesIO(big.content)).size) == 2000
    assert "max-age" in r.headers["cache-control"]
    if "oferta.pdf" in ids:
        assert admin.get(f"/files/{ids['oferta.pdf']}/thumb").status_code == 200
        v = admin.get(f"/files/{ids['oferta.pdf']}/view")
        assert v.status_code == 200 and v.headers["content-disposition"].startswith("inline")
        assert v.headers["x-frame-options"] == "SAMEORIGIN"
    # HTML i SVG nigdy nie są wyświetlane w przeglądarce (ochrona przed XSS) — tylko pobranie
    assert admin.get(f"/files/{ids['strona.html']}/view").status_code == 404
    assert admin.get(f"/files/{ids['grafika.svg']}/view").status_code == 404
    assert admin.get(f"/files/{ids['strona.html']}/thumb").status_code == 404
    if sh.which("ffmpeg"):
        vid = app.state.settings.tmp_dir / "klip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=orange:s=320x240:d=2", "-pix_fmt", "yuv420p",
                        str(vid)], check=True, timeout=60)
        admin.post("/admin/clients/2/upload", data={"csrf": tok}, files=[("files", ("klip.mp4", vid.read_bytes()))])
        vid_id = app.state.db.one("SELECT id FROM files WHERE original_name = 'klip.mp4'")["id"]
        assert admin.get(f"/files/{vid_id}/thumb").status_code == 200
        rng = admin.get(f"/files/{vid_id}/view", headers={"Range": "bytes=0-99"})
        assert rng.status_code == 206
    page = admin.get("/admin/clients/2").text
    assert 'data-kind="image"' in page and "data-lightbox" in page


def test_search_scope(app, admin):
    create_client_account(admin, "a.klient", "KlientHaslo1")
    create_client_account(admin, "b.klient", "KlientHaslo2")
    tok = csrf_of(admin, "/admin/clients/2")
    admin.post("/admin/clients/2/upload", data={"csrf": tok}, files=[("files", ("logo_final_A.ai", b"x"))])
    admin.post("/admin/clients/3/upload", data={"csrf": tok}, files=[("files", ("logo_final_B.ai", b"x"))])
    r = admin.get("/search", params={"q": "logo_final"}).text
    assert "logo_final_A.ai" in r and "logo_final_B.ai" in r
    b = TestClient(app)
    login(b, "b.klient", "KlientHaslo2")
    change_pw(b, "KlientHaslo2")
    r = b.get("/search", params={"q": "logo"}).text
    assert "logo_final_B.ai" in r and "logo_final_A.ai" not in r
    assert "Wpisz co najmniej 2 znaki" in b.get("/search", params={"q": "l"}).text
    assert "Nic nie znaleziono" in b.get("/search", params={"q": "%%"}).text  # % to zwykły znak, nie „wszystko”


def test_username_cannot_start_with_underscore(app, admin):
    tok = csrf_of(admin, "/admin/clients/new")
    r = admin.post("/admin/clients/new", data={"username": "_prywatne", "password": "KlientHaslo1", "csrf": tok})
    assert "Login" in r.text and not app.state.db.one("SELECT 1 FROM users WHERE username = '_prywatne'")


def test_pwa_manifest_service_worker_and_icons(app):
    c = TestClient(app)
    m = c.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    data = m.json()
    assert data["display"] == "standalone" and data["start_url"].startswith("/")
    for icon in data["icons"]:
        r = c.get(icon["src"])
        assert r.status_code == 200 and r.content[:4] == b"\x89PNG", icon["src"]
    sw = c.get("/sw.js")
    assert sw.headers["content-type"].startswith("text/javascript") and "__VERSION__" not in sw.text
    version = re.search(r'/static/style\.css\?v=([0-9a-f]{10})"', c.get("/login").text).group(1)
    assert version in sw.text and sw.headers["service-worker-allowed"] == "/"
    assert "Brak połączenia" in c.get("/offline").text
    page = c.get("/login").text
    assert 'rel="manifest"' in page and "apple-touch-icon" in page and "Zapamiętaj mnie" in page


def test_remember_me_session_lifetime(app):
    import time as _t
    from itsdangerous import TimestampSigner
    import base64
    import json as _json

    def session_exp(client):
        raw = client.cookies.get("bsk_session")
        data = TimestampSigner("x" * 40).unsign(raw.encode())
        return _json.loads(base64.b64decode(data))["exp"]

    short = TestClient(app)
    tok = csrf_of(short, "/login")
    short.post("/login", data={"username": "admin", "password": ADMIN_PW, "csrf": tok})
    assert 11 * 3600 < session_exp(short) - _t.time() <= 12 * 3600
    long = TestClient(app)
    tok = csrf_of(long, "/login")
    long.post("/login", data={"username": "admin", "password": ADMIN_PW, "csrf": tok, "remember": "1"})
    assert 29 * 86400 < session_exp(long) - _t.time() <= 30 * 86400
    assert long.get("/admin").status_code == 200


def test_expired_session_logs_out(app, monkeypatch):
    import time as _t
    c = TestClient(app)
    login(c, "admin", ADMIN_PW)
    assert c.get("/admin", follow_redirects=False).status_code == 200
    real = _t.time
    monkeypatch.setattr("app.main.time.time", lambda: real() + 13 * 3600)
    assert c.get("/admin", follow_redirects=False).headers["location"] == "/login"


def test_company_members_share_space(app, admin):
    create_client_account(admin)  # jan.kowalski = konto główne firmy (id 2)
    tok = csrf_of(admin, "/admin/clients/2")
    r = admin.post("/admin/clients/2/members", data={"csrf": tok, "username": "piotr.acme", "display_name": "Piotr", "email": ""})
    pw = re.search(r"Hasło: (\S+)\"", r.text).group(1)
    assert "piotr.acme" in r.text and "Osoby z dostępem" in r.text
    assert "piotr.acme" not in admin.get("/admin/clients").text  # nie jest osobnym klientem
    # wspólna przestrzeń: plik od Piotra widzi Jan i admin; wiadomość ma autora
    p = TestClient(app)
    login(p, "piotr.acme", pw)
    change_pw(p, pw)
    ptok = csrf_of(p, "/files")
    p.post("/files/upload", data={"csrf": ptok}, files=[("files", ("od_piotra.png", _png()))])
    p.post("/files/message", data={"csrf": ptok, "body": "Pozdrawiam, Piotr"})
    assert (app.state.settings.clients_dir / "jan.kowalski" / "od_piotra.png").exists()
    j = TestClient(app)
    login(j, "jan.kowalski", "KlientHaslo1")
    change_pw(j, "KlientHaslo1")
    page = j.get("/files").text
    assert "od_piotra.png" in page and "Pozdrawiam, Piotr" in page
    assert j.get(f"/files/1/download").status_code == 200
    assert admin.get("/api/unread").json()["unread"] >= 2
    assert "od_piotra.png" in admin.get("/admin/clients/2").text
    # blokada i usunięcie dostępu
    mid = app.state.db.one("SELECT id FROM users WHERE username = 'piotr.acme'")["id"]
    admin.post(f"/admin/members/{mid}/toggle", data={"csrf": tok})
    assert p.get("/files", follow_redirects=False).status_code == 303
    admin.post(f"/admin/members/{mid}/delete", data={"csrf": tok})
    assert "piotr.acme" not in admin.get("/admin/clients/2").text
    assert "Pozdrawiam, Piotr" in admin.get("/admin/clients/2").text  # historia zostaje
    assert admin.get(f"/admin/clients/{mid}").status_code == 404


def test_review_pins_and_status(app, admin):
    create_client_account(admin)
    tok = csrf_of(admin, "/admin/clients/2")
    admin.post("/admin/clients/2/upload", data={"csrf": tok, "review": "1"}, files=[("files", ("projekt_logo.png", _png()))])
    f = app.state.db.one("SELECT * FROM files")
    assert f["review_status"] == "review"
    assert "Do akceptacji" in admin.get("/admin/clients/2").text
    c = TestClient(app)
    login(c, "jan.kowalski", "KlientHaslo1")
    change_pw(c, "KlientHaslo1")
    h = {"X-CSRF-Token": csrf_of(c, "/files")}
    data = c.get(f"/api/files/{f['id']}/review", headers=h).json()
    assert data["status"] == "review" and data["preview"] and not data["is_admin"]
    # pinezka na obrazie + zwykły komentarz
    data = c.post(f"/api/files/{f['id']}/comments", json={"x": 0.25, "y": 0.6, "body": "Tu zmienić kolor"}, headers=h).json()
    data = c.post(f"/api/files/{f['id']}/comments", json={"body": "Ogólnie super"}, headers=h).json()
    pins = [x for x in data["comments"] if x["x"] is not None]
    assert len(pins) == 1 and pins[0]["n"] == 1 and pins[0]["body"] == "Tu zmienić kolor"
    assert c.post(f"/api/files/{f['id']}/comments", json={"x": 2, "y": 0.5, "body": "x"}, headers=h).status_code == 400
    assert c.post(f"/api/files/{f['id']}/comments", json={"body": "x"}).status_code == 400  # bez CSRF
    # klient prosi o poprawki -> status + wiadomość w rozmowie
    data = c.post(f"/api/files/{f['id']}/status", json={"status": "changes"}, headers=h).json()
    assert data["status"] == "changes" and data["by"]
    assert c.post(f"/api/files/{f['id']}/status", json={"status": "review"}, headers=h).status_code == 400  # tylko admin
    thread = admin.get("/api/thread/2").json()["messages"]
    assert any("poprawki" in m["body"] for m in thread) and any("Tu zmienić kolor" in m["body"] for m in thread)
    # admin wgrywa nową wersję -> wraca do akceptacji, stare pinezki oznaczone jako starsza wersja
    admin.post("/admin/clients/2/upload", data={"csrf": tok}, files=[("files", ("projekt_logo.png", _png(color=(0, 0, 0))))])
    ah = {"X-CSRF-Token": csrf_of(admin, "/admin/clients/2")}
    data = admin.get(f"/api/files/{f['id']}/review", headers=ah).json()
    assert data["status"] == "review" and data["version"] == 2 and all(x["old"] for x in data["comments"])
    data = c.post(f"/api/files/{f['id']}/status", json={"status": "approved"}, headers=h).json()
    assert data["status"] == "approved"
    cid = data["comments"][0]["id"]
    assert c.post(f"/api/files/{f['id']}/comments/{cid}/resolve", json={}, headers=h).json()["comments"][0]["resolved"]
    # obcy klient nie ma dostępu
    create_client_account(admin, "obcy.klient", "KlientHaslo3")
    o = TestClient(app)
    login(o, "obcy.klient", "KlientHaslo3")
    change_pw(o, "KlientHaslo3")
    oh = {"X-CSRF-Token": csrf_of(o, "/files")}
    assert o.get(f"/api/files/{f['id']}/review", headers=oh).status_code == 404


def test_transfer_like_wetransfer(mail_app):
    import io
    import zipfile as zf
    app, sent = mail_app, mail_app.state.sent
    admin = TestClient(app)
    login(admin, "admin", ADMIN_PW)
    tok = csrf_of(admin, "/admin/transfers")
    r = admin.post("/admin/transfers", data={"csrf": tok, "title": "Kampania — druk", "message": "Pliki do druku",
                                              "recipients": "druk@drukarnia.pl, anna@acme.pl", "expires_days": 7},
                   files=[("files", ("plakat.png", _png())), ("files", ("opis.txt", b"tekst")),
                          ("files", ("opis.txt", b"drugi"))])
    url = re.search(r'value="(https://cloud\.bskmedia\.pl/t/[^"]+)"', r.text).group(1)
    path = url.replace("https://cloud.bskmedia.pl", "")
    assert wait_for_mail(sent, 2) and {s[0] for s in sent[:2]} == {"druk@drukarnia.pl", "anna@acme.pl"}
    assert path in sent[0][2] and "Pliki do druku" in sent[0][2]
    anon = TestClient(app)
    page = anon.get(path)
    assert "Kampania — druk" in page.text and "Pobierz wszystko (ZIP)" in page.text and "3 pliki" in page.text
    # ZIP z wszystkimi plikami (powtórzona nazwa dostaje numer)
    z = zf.ZipFile(io.BytesIO(anon.get(path + "/zip").content))
    assert sorted(z.namelist()) == ["opis (2).txt", "opis.txt", "plakat.png"] and z.read("opis (2).txt") == b"drugi"
    tf = app.state.db.all("SELECT * FROM transfer_files ORDER BY id")
    assert anon.get(f"{path}/f/{tf[1]['id']}").content == b"tekst"
    assert anon.get(f"{path}/thumb/{tf[0]['id']}").headers["content-type"] == "image/webp"
    assert anon.get(f"{path}/f/{tf[1]['id']}", headers={"Range": "bytes=2-"}).status_code == 206
    t = app.state.db.one("SELECT * FROM transfers")
    assert t["downloads"] == 2  # ZIP + 1 plik (wznowienie się nie liczy)
    assert wait_for_mail(sent, 3) and "Pobrano transfer" in sent[2][1]
    assert "2×" in admin.get("/admin/transfers").text
    # wygaśnięcie i wyłączenie
    app.state.db.run("UPDATE transfers SET expires_at = '2020-01-01T00:00:00+00:00'")
    assert "wygasł" in anon.get(path).text and anon.get(path + "/zip").status_code == 410
    app.state.db.run("UPDATE transfers SET expires_at = ?", "2999-01-01T00:00:00+00:00")
    admin.post(f"/admin/transfers/{t['id']}/revoke", data={"csrf": tok})
    assert "wyłączony" in anon.get(path).text
    admin.post(f"/admin/transfers/{t['id']}/delete", data={"csrf": tok})
    assert anon.get(path).status_code == 404
    assert not (app.state.settings.transfers_dir / str(t["id"])).exists()


def test_transfer_password_and_backgrounds(app, admin):
    tok = csrf_of(admin, "/admin/transfers")
    admin.post("/admin/transfers/backgrounds", data={"csrf": tok},
               files=[("files", ("realizacja.jpg", _png())), ("files", ("zly.html", b"<x>"))])
    assert (app.state.settings.backgrounds_dir / "realizacja.jpg").exists()
    assert not (app.state.settings.backgrounds_dir / "zly.html").exists()
    r = admin.post("/admin/transfers", data={"csrf": tok, "password": "tajne123", "expires_days": 3},
                   files=[("files", ("a.txt", b"A"))])
    path = "/t/" + re.search(r'/t/([A-Za-z0-9_-]+)"', r.text).group(1)
    anon = TestClient(app)
    page = anon.get(path).text
    assert "Hasło do transferu" in page and "/t/tlo/realizacja.jpg" in page and "a.txt" not in page
    fid = app.state.db.one("SELECT id FROM transfer_files")["id"]
    assert anon.get(f"{path}/f/{fid}", follow_redirects=False).status_code == 303
    t2 = csrf_of(anon, path)
    anon.post(path, data={"csrf": t2, "password": "zle"})
    anon.post(path, data={"csrf": t2, "password": "tajne123"})
    assert anon.get(f"{path}/f/{fid}").content == b"A"
    assert anon.get("/t/tlo/..%2Fapp%2Fbskcloud.db").status_code == 404


def _chunk_upload(c, h, name, data, dir_=""):
    up = c.post("/api/uploads", json={"name": name, "size": len(data), "dir": dir_}, headers=h).json()
    if data:
        c.put(f"/api/uploads/{up['id']}", params={"offset": 0}, content=data, headers=h)
    return up["id"]


def test_folder_upload_keeps_structure(app, admin):
    tok = csrf_of(admin, "/admin/my")
    h = {"X-CSRF-Token": tok}
    ids = [_chunk_upload(admin, h, "IMG_001.jpg", _png(), "Grecja 2021"),
           _chunk_upload(admin, h, "IMG_002.jpg", _png(), "Grecja 2021/Dzień 2"),
           _chunk_upload(admin, h, "Thumbs.db", b"x", "Grecja 2021"),
           _chunk_upload(admin, h, "licencja.txt", b"KEY", "Licencje")]
    assert admin.post("/api/uploads", json={"name": "x", "size": 1, "dir": "../../etc"}, headers=h).status_code == 400
    r = admin.post("/admin/my/upload", data={"csrf": tok, "upload_ids": ids})
    assert "Zapisano plików na Twoim dysku: 3" in r.text
    names = {r["name"]: r for r in app.state.db.all("SELECT * FROM folders WHERE owner_id = 1")}
    assert set(names) == {"Grecja 2021", "Dzień 2", "Licencje"}
    assert names["Dzień 2"]["parent_id"] == names["Grecja 2021"]["id"]
    base = app.state.settings.clients_dir / "_prywatne" / "admin"
    assert (base / "Grecja 2021" / "Dzień 2" / "IMG_002.jpg").exists() and (base / "Licencje" / "licencja.txt").exists()
    assert not list(base.rglob("Thumbs.db"))
    # druga wysyłka tego samego folderu trafia do istniejących katalogów (bez duplikatów)
    ids = [_chunk_upload(admin, h, "IMG_003.jpg", _png(), "grecja 2021")]
    admin.post("/admin/my/upload", data={"csrf": tok, "upload_ids": ids})
    assert app.state.db.one("SELECT COUNT(*) AS n FROM folders WHERE owner_id = 1")["n"] == 3
    page = admin.get(f"/admin/my?folder={names['Grecja 2021']['id']}").text
    assert "IMG_001.jpg" in page and "IMG_003.jpg" in page and "Dzień 2" in page


def test_bulk_trash_and_move(app, admin):
    create_client_account(admin)
    tok = csrf_of(admin, "/admin/clients/2")
    admin.post("/spaces/2/folders", data={"csrf": tok, "name": "Archiwum"})
    folder = app.state.db.one("SELECT id FROM folders")["id"]
    admin.post("/admin/clients/2/upload", data={"csrf": tok},
               files=[("files", (f"plik{i}.txt", b"x")) for i in range(5)])
    ids = [r["id"] for r in app.state.db.all("SELECT id FROM files ORDER BY id")]
    r = admin.post("/files/bulk", data={"csrf": tok, "ids": ids[:3], "action": "move", "folder_id": folder,
                                        "back": "/admin/clients/2"})
    assert "Przeniesiono: 3 plików" in r.text
    assert app.state.db.one("SELECT COUNT(*) AS n FROM files WHERE folder_id = ?", folder)["n"] == 3
    assert (app.state.settings.clients_dir / "jan.kowalski" / "Archiwum" / "plik0.txt").exists()
    r = admin.post("/files/bulk", data={"csrf": tok, "ids": ids, "action": "trash", "back": "/admin/clients/2"})
    assert "Przeniesiono do kosza: 5 plików" in r.text
    assert app.state.db.one("SELECT COUNT(*) AS n FROM files WHERE deleted_at IS NULL")["n"] == 0
    # klient: tylko własne pliki, bez przenoszenia
    for f in ids:
        app.state.db.run("UPDATE files SET deleted_at = NULL WHERE id = ?", f)
    c = TestClient(app)
    login(c, "jan.kowalski", "KlientHaslo1")
    change_pw(c, "KlientHaslo1")
    ctok = csrf_of(c, "/files")
    c.post("/files/upload", data={"csrf": ctok}, files=[("files", ("moj.txt", b"m"))])
    mine = app.state.db.one("SELECT id FROM files WHERE original_name = 'moj.txt'")["id"]
    r = c.post("/files/bulk", data={"csrf": ctok, "ids": [mine, ids[0]], "action": "trash", "back": "/files"})
    assert "Przeniesiono do kosza: 1 plików. Pominięto 1" in r.text
    assert c.post("/files/bulk", data={"csrf": ctok, "ids": [mine], "action": "move", "back": "/files"}).status_code == 403
    assert admin.post("/files/bulk", data={"csrf": tok, "ids": [mine], "action": "zly"}).status_code == 400
    assert 'data-bulkbar' in c.get("/files").text and "Zaznacz" in c.get("/files").text


def test_big_upload_saves_each_file_immediately_and_resumes(app, admin):
    tok = csrf_of(admin, "/admin/my")
    h = {"X-CSRF-Token": tok}
    dest = {"kind": "my", "folder_id": None}
    data = _png()

    def send(name, payload, dir_="", mtime=1700000000000):
        r = admin.post("/api/uploads", json={"name": name, "size": len(payload), "dir": dir_, "mtime": mtime, "dest": dest},
                       headers=h).json()
        if r.get("status") in ("exists", "stored"):
            return r
        return admin.put(f"/api/uploads/{r['id']}", params={"offset": 0}, content=payload, headers=h).json()

    r = send("zdjecie.png", data, "Archiwum/2019")
    assert r["status"] == "stored" and r["file_id"]  # zapisany od razu, bez kończącego formularza
    assert app.state.db.one("SELECT COUNT(*) AS n FROM files")["n"] == 1
    assert (app.state.settings.clients_dir / "_prywatne" / "admin" / "Archiwum" / "2019" / "zdjecie.png").exists()
    # ponowne przeciągnięcie tego samego folderu: ten plik zostaje pominięty
    assert send("zdjecie.png", data, "Archiwum/2019")["status"] == "exists"
    # inna data modyfikacji = zmieniony plik -> nowa wersja
    assert send("zdjecie.png", data, "Archiwum/2019", mtime=1800000000000)["status"] == "stored"
    assert app.state.db.one("SELECT version FROM files")["version"] == 2
    assert send("pusty.txt", b"")["status"] == "stored"
    r = admin.post("/api/uploads/finish", json={"dest": dest, "stored": 3, "skipped": 1, "failed": ["zly.psd"], "names": []},
                   headers=h).json()
    assert r["redirect"].startswith("/admin/my")
    page = admin.get(r["redirect"]).text
    assert "Zapisano plików: 3" in page and "Pominięto 1" in page and "zly.psd" in page
    # klient nie może pisać do „Mojego dysku” ani do innego klienta
    create_client_account(admin)
    c = TestClient(app)
    login(c, "jan.kowalski", "KlientHaslo1")
    change_pw(c, "KlientHaslo1")
    ch = {"X-CSRF-Token": csrf_of(c, "/files")}
    assert c.post("/api/uploads", json={"name": "x", "size": 1, "dest": {"kind": "my"}}, headers=ch).status_code == 403
    assert c.post("/api/uploads", json={"name": "x", "size": 1, "dest": {"kind": "client", "client_id": 2}}, headers=ch).status_code == 403
    r = c.post("/api/uploads", json={"name": "od_klienta.png", "size": len(data), "mtime": 1, "dest": {"kind": "self"}}, headers=ch).json()
    r = c.put(f"/api/uploads/{r['id']}", params={"offset": 0}, content=data, headers=ch).json()
    assert r["status"] == "stored"
    assert (app.state.settings.clients_dir / "jan.kowalski" / "od_klienta.png").exists()


def test_import_from_nas_folder(app, admin):
    import time
    s = app.state.settings
    src = s.import_dir / "Grecja 2021" / "Dzień 2"
    src.mkdir(parents=True)
    (src / "zachod.jpg").write_bytes(_png())
    (s.import_dir / "Grecja 2021" / "plaza.png").write_bytes(_png())
    (s.import_dir / "Grecja 2021" / "Thumbs.db").write_bytes(b"x")
    (s.import_dir / "faktura.pdf").write_bytes(b"%PDF")
    data = admin.get("/api/import").json()
    names = {e["name"]: e for e in data["entries"]}
    assert names["Grecja 2021"]["files"] == 2 and names["faktura.pdf"]["files"] == 1
    tok = csrf_of(admin, "/admin/my")
    r = admin.post("/admin/import", data={"csrf": tok, "owner_id": 1, "names": ["Grecja 2021", "faktura.pdf", "../app"]})
    assert "Import ruszył" in r.text
    for _ in range(100):
        job = admin.get("/api/import").json()["job"]
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["total_files"] == 3 and job["done_files"] == 3
    base = s.clients_dir / "_prywatne" / "admin"
    assert (base / "Grecja 2021" / "Dzień 2" / "zachod.jpg").exists() and (base / "faktura.pdf").exists()
    assert not (s.import_dir / "Grecja 2021" / "Dzień 2").exists()  # puste katalogi posprzątane
    assert app.state.db.one("SELECT COUNT(*) AS n FROM files")["n"] == 3
    fid = app.state.db.one("SELECT id FROM files WHERE original_name = 'zachod.jpg'")["id"]
    assert admin.get(f"/files/{fid}/thumb").status_code == 200
    # ponowny import tych samych plików -> pominięte, nic nie nadpisane
    (s.import_dir / "faktura.pdf").write_bytes(b"%PDF")
    admin.post("/admin/import", data={"csrf": tok, "owner_id": 1, "names": ["faktura.pdf"]})
    for _ in range(100):
        job = admin.get("/api/import").json()["job"]
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["skipped"] == 1 and app.state.db.one("SELECT COUNT(*) AS n FROM files")["n"] == 3
    # klient nie ma dostępu do importu
    create_client_account(admin)
    c = TestClient(app)
    login(c, "jan.kowalski", "KlientHaslo1")
    assert c.get("/api/import", follow_redirects=False).status_code in (303, 403)


def test_connection_indicator(app, admin):
    assert admin.get("/api/connection").json() == {"lan": True}
    assert admin.get("/api/connection", headers={"CF-Ray": "abc-WAW"}).json() == {"lan": False}
