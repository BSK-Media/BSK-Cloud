import hashlib
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
