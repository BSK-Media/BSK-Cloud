"""Test end-to-end: prawdziwy serwer (uvicorn w wątku) + agent obserwujący folder."""
import os
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


def test_big_file_chunked_and_lan_fallback(tmp_path, server, monkeypatch):
    """Duży plik idzie w kawałkach; martwy adres biurowy -> automatycznie przez adres publiczny."""
    from app import chunks
    url, token, settings, _ = server
    monkeypatch.setattr(bsk_backup, "CHUNK_THRESHOLD", 10_000)
    monkeypatch.setattr(chunks, "CHUNK_SIZE", 7_000)
    watched = tmp_path / "Projekty"
    watched.mkdir()
    big = watched / "render.mp4"
    payload = os.urandom(50_000)
    big.write_bytes(payload)
    os.utime(big, (time.time() - 60, time.time() - 60))
    agent, state = make_agent(tmp_path, url, token, watched)
    agent.cfg.lan_server = f"http://127.0.0.1:{free_port()}"  # nikt tam nie słucha
    puts = []
    orig_put = agent.http.put
    agent.http.put = lambda *a, **k: (puts.append(a[0]), orig_put(*a, **k))[1]
    agent.scan()
    agent.work_once()
    assert (settings.backups_dir / "pc-test" / "Projekty" / "render.mp4").read_bytes() == payload
    assert len(puts) == 8 and all("/api/agent/uploads/" in p for p in puts)  # 50 000 / 7 000 -> 8 kawałków
    assert agent.base() == url


def test_lan_preferred_when_available(tmp_path, server):
    url, token, settings, _ = server
    watched = tmp_path / "Biuro"
    watched.mkdir()
    f = watched / "a.txt"
    f.write_text("z biura")
    os.utime(f, (time.time() - 60, time.time() - 60))
    agent, state = make_agent(tmp_path, f"http://127.0.0.1:{free_port()}", token, watched)  # publiczny niedostępny
    agent.cfg.lan_server = url
    agent.scan()
    agent.work_once()
    assert (settings.backups_dir / "pc-test" / "Biuro" / "a.txt").read_text() == "z biura"


def test_folders_from_admin_panel(tmp_path, server):
    """Admin ustawia foldery w panelu -> agent je pobiera i zaczyna kopiować."""
    url, token, settings, _ = server
    from app.db import Database  # noqa: F401
    projekty = tmp_path / "Projekty"
    projekty.mkdir()
    (projekty / "plan.txt").write_text("plan")
    os.utime(projekty / "plan.txt", (time.time() - 60, time.time() - 60))
    import sqlite3
    con = sqlite3.connect(settings.db_path)
    con.execute("UPDATE devices SET folders = ? WHERE name = 'PC-TEST'", (f"{projekty}\n@nieznany",))
    con.commit()
    con.close()
    start = tmp_path / "Stary"
    start.mkdir()
    agent, state = make_agent(tmp_path, url, token, start)
    assert agent.refresh_remote_config() is True
    assert [str(r) for r, _ in agent.roots] == [str(projekty)]
    assert agent.refresh_remote_config() is False  # bez zmian
    agent.scan()
    agent.work_once()
    assert (settings.backups_dir / "pc-test" / "Projekty" / "plan.txt").read_text() == "plan"


def test_resolve_special_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(bsk_backup.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "OneDrive" / "Pulpit").mkdir(parents=True)
    (tmp_path / "Documents").mkdir()
    assert bsk_backup.resolve_folder("@pulpit") == tmp_path / "OneDrive" / "Pulpit"
    assert bsk_backup.resolve_folder("@dokumenty") == tmp_path / "Documents"
    assert bsk_backup.resolve_folder("@obrazy") is None
    assert bsk_backup.resolve_folder("@cos") is None


def test_sync_from_panel_and_report(tmp_path, server):
    """„Synchronizuj teraz” z panelu -> skan, raport z dziennikiem i potwierdzenie wykonania."""
    import json
    import sqlite3
    url, token, settings, _ = server
    watched = tmp_path / "Pulpit"
    watched.mkdir()
    agent, state = make_agent(tmp_path, url, token, watched)
    t = threading.Thread(target=agent.run, daemon=True)
    t.start()
    try:
        assert wait_for(lambda: agent._config_checked > 0)
        (watched / "skan.txt").write_text("plik do skanu")
        os.utime(watched / "skan.txt", (time.time() - 60, time.time() - 60))
        bsk_backup.log.warning("Testowe ostrzeżenie z komputera")
        con = sqlite3.connect(settings.db_path)
        con.execute("UPDATE devices SET sync_requested = '2026-11-09T10:00:00+00:00' WHERE name = 'PC-TEST'")
        con.commit()
        agent._config_checked = 0  # nie czekaj 2 minut w teście
        backup = settings.backups_dir / "pc-test" / "Pulpit" / "skan.txt"
        assert wait_for(lambda: backup.exists()), "plik wysłany po synchronizacji"

        def row():
            return con.execute("SELECT sync_done, agent_report, agent_version FROM devices WHERE name = 'PC-TEST'").fetchone()
        assert wait_for(lambda: row()[0] == "2026-11-09T10:00:00+00:00"), "potwierdzenie synchronizacji"
        report = json.loads(row()[1])
        assert any("Testowe ostrzeżenie" in e["msg"] for e in report["log"])
        assert row()[2] == bsk_backup.VERSION
        assert agent.cfg.last_sync == "2026-11-09T10:00:00+00:00"
        con.close()
    finally:
        agent.stop.set()
        t.join(10)


def test_self_update_downloads_and_verifies(tmp_path, server, monkeypatch):
    """Nowsza wersja w bsk-dane/instalator -> pobranie, sprawdzenie sumy, uruchomienie aktualizatora."""
    url, token, settings, _ = server
    inst = settings.installer_dir
    inst.mkdir(parents=True, exist_ok=True)
    (inst / "bsk-backup-agent.exe").write_bytes(b"NOWY-AGENT")
    (inst / "bsk-backup.exe").write_bytes(b"NOWY-CLI")
    (inst / "version.txt").write_text("9.0.0")
    watched = tmp_path / "Docs"
    watched.mkdir()
    agent, state = make_agent(tmp_path, url, token, watched)
    launched = []
    monkeypatch.setattr(bsk_backup.Agent, "can_self_update", staticmethod(lambda: True))
    monkeypatch.setattr(agent, "launch_updater", lambda staged: launched.append(staged))
    agent.refresh_remote_config()
    assert launched and launched[0]["bsk-backup-agent.exe"].read_bytes() == b"NOWY-AGENT"
    assert agent.restart_for_update and agent.stop.is_set()
    # ta sama wersja nie jest pobierana w kółko
    agent.stop.clear()
    agent.restart_for_update = False
    agent.refresh_remote_config()
    assert len(launched) == 1 and not agent.restart_for_update

    # zła suma kontrolna -> brak aktualizacji, błąd w raporcie
    agent2, _ = make_agent(tmp_path / "b", url, token, watched)
    monkeypatch.setattr(agent2, "launch_updater", lambda staged: launched.append(staged))
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        agent2.download_update({"bsk-backup-agent.exe": "0" * 64})
    assert agent2.maybe_update({"version": "9.0.0", "files": {"bsk-backup-agent.exe": "0" * 64}}) is False
    assert "suma kontrolna" in agent2.update_error and len(launched) == 1


def test_updater_script_quotes_paths(tmp_path, monkeypatch):
    """Skrypt aktualizacji: BOM, ścieżki z polskimi znakami i apostrofem w cudzysłowach PowerShell."""
    calls = []
    monkeypatch.setattr(bsk_backup.subprocess, "Popen", lambda *a, **k: calls.append(a[0]))
    install = tmp_path / "Programs" / "O'Brien Łukasz" / "BSKBackup"
    install.mkdir(parents=True)
    monkeypatch.setattr(bsk_backup.sys, "executable", str(install / "bsk-backup-agent.exe"))
    staged_dir = tmp_path / "aktualizacja"
    staged_dir.mkdir()
    staged = {"bsk-backup-agent.exe": staged_dir / "bsk-backup-agent.exe.new"}
    agent, _ = make_agent(tmp_path, "http://127.0.0.1:1", "t", tmp_path)
    agent.launch_updater(staged)
    script = staged_dir / "aktualizuj.ps1"
    data = script.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert "O''Brien Łukasz" in text and f"Wait-Process -Id {os.getpid()}" in text
    assert calls and calls[0][-1] == str(script)
