import hashlib
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
