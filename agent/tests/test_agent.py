"""Test end-to-end: prawdziwy serwer (uvicorn w wątku) + agent obserwujący folder."""
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "agent"))

import bsk_backup  # noqa: E402
from app import security  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import now_iso  # noqa: E402
from app.main import create_app  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


@pytest.fixture()
def server(tmp_path):
    settings = Settings(data_dir=tmp_path / "nas", secret_key="s" * 40, cookie_secure=False)
    app = create_app(settings)
    token = "bsk_testtoken"
    app.state.db.run(
        "INSERT INTO devices (name, employee, folder_name, token_hash, created_at) VALUES (?,?,?,?,?)",
        "PC-TEST", "Test", "pc-test", security.token_hash(token), now_iso(),
    )
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    assert wait_for(lambda: srv.started)
    yield f"http://127.0.0.1:{port}", token, settings, srv
    srv.should_exit = True
    t.join(5)


def make_agent(tmp_path, url, token, watched):
    cfg = bsk_backup.Config({"server": url, "token": token, "folders": bsk_backup.make_labels([watched]),
                             "settle_seconds": 0.5, "scan_interval_minutes": 60}, tmp_path / "cfg" / "config.json")
    state = bsk_backup.State(tmp_path / "cfg" / "state.db")
    return bsk_backup.Agent(cfg, state), state


def test_watch_and_upload(tmp_path, server):
    url, token, settings, _ = server
    watched = tmp_path / "Dokumenty"
    (watched / "Projekt").mkdir(parents=True)
    (watched / "stary.txt").write_text("istniał przed startem")
    agent, state = make_agent(tmp_path, url, token, watched)
    t = threading.Thread(target=agent.run, daemon=True)
    t.start()
    try:
        backup = settings.backups_dir / "pc-test" / "Dokumenty"
        assert wait_for(lambda: (backup / "stary.txt").exists()), "skan początkowy"
        time.sleep(0.5)
        (watched / "Projekt" / "nowy.psd").write_bytes(b"A" * 5000)
        (watched / "~$tymczasowy.docx").write_text("x")
        (watched / "Thumbs.db").write_text("x")
        assert wait_for(lambda: (backup / "Projekt" / "nowy.psd").exists()), "nowy plik"
        time.sleep(1.2)
        (watched / "Projekt" / "nowy.psd").write_bytes(b"B" * 6000)
        assert wait_for(lambda: (backup / "Projekt" / "nowy.psd").read_bytes() == b"B" * 6000), "zapis zmiany"
        assert wait_for(lambda: any((settings.backups_dir / "pc-test" / ".wersje").rglob("nowy.psd.*")))
        assert not (backup / "~$tymczasowy.docx").exists()
        assert not (backup / "Thumbs.db").exists()
    finally:
        agent.stop.set()
        t.join(10)


def test_offline_queue(tmp_path):
    """Serwer niedostępny: plik czeka w kolejce, po powrocie serwera zostaje wysłany."""
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    watched = tmp_path / "Pulpit"
    watched.mkdir()
    f = watched / "oferta.pdf"
    f.write_bytes(b"PDF")
    import os
    os.utime(f, (time.time() - 60, time.time() - 60))
    agent, state = make_agent(tmp_path, url, "bsk_testtoken", watched)
    agent.scan()
    agent.work_once()
    assert state.counts()["pending"] == 1 and agent.paused_until > time.time()

    settings = Settings(data_dir=tmp_path / "nas", secret_key="s" * 40, cookie_secure=False)
    app = create_app(settings)
    app.state.db.run(
        "INSERT INTO devices (name, employee, folder_name, token_hash, created_at) VALUES (?,?,?,?,?)",
        "PC", "", "pc", security.token_hash("bsk_testtoken"), now_iso(),
    )
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    try:
        assert wait_for(lambda: srv.started)
        agent.paused_until = 0
        agent.work_once()
        assert (settings.backups_dir / "pc" / "Pulpit" / "oferta.pdf").read_bytes() == b"PDF"
        assert state.counts()["pending"] == 0
    finally:
        srv.should_exit = True
        t.join(5)


def test_bad_token_pauses(tmp_path, server):
    url, _, _, _ = server
    watched = tmp_path / "Docs"
    watched.mkdir()
    f = watched / "a.txt"
    f.write_text("a")
    import os
    os.utime(f, (time.time() - 60, time.time() - 60))
    agent, state = make_agent(tmp_path, url, "bsk_zly", watched)
    agent.scan()
    agent.work_once()
    assert state.counts()["pending"] == 1
    assert agent.paused_until > time.time() + 600
