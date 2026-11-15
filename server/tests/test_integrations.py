"""Integracje agenta: WordPress/WooCommerce, Search Console, PageSpeed, sieć (SNMP), ocena leadów, raport."""
import base64
import json
import re
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app import ai, gsc, integrations, monitor, netmon, report, wpcheck
from app.config import Settings
from app.main import create_app
from app.secretbox import SecretBox


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class Base(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype="application/json"):
        data = (json.dumps(obj) if not isinstance(obj, str) else obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def auth(self):
        h = self.headers.get("Authorization", "")
        return base64.b64decode(h[6:]).decode() if h.startswith("Basic ") else ""


class WP(Base):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/wp-json/":
            return self.send({"name": "Sklep", "namespaces": ["oembed/1.0", "wp/v2", "wc/v3"]})
        if path == "/feed/":
            return self.send("<rss><channel><generator>https://wordpress.org/?v=6.4.2</generator></channel></rss>", ctype="text/xml")
        if path == "/wp-json/wp/v2/plugins":
            if self.auth() != "admin:abcd efgh":
                return self.send({"code": "rest_forbidden"}, 401)
            return self.send([{"plugin": "akismet/akismet", "name": "Akismet", "version": "5.0", "status": "active"},
                              {"plugin": "premium/premium", "name": "Premium", "version": "1.0", "status": "active"},
                              {"plugin": "stary/stary", "name": "Stary", "version": "1.0", "status": "inactive"}])
        if path == "/wp-json/wc/v3/system_status":
            if self.auth() != "ck_1:cs_1":
                return self.send({"code": "woocommerce_rest_cannot_view"}, 401)
            return self.send({"environment": {"php_version": "7.4.33", "wp_version": "6.4.2"}, "active_plugins": []})
        if path == "/wp-json/wc/v3/orders":
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            def o(h, st):
                return {"id": h, "status": st, "date_created_gmt": (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S")}
            return self.send([o(2, "failed"), o(3, "processing")] + [o(30 + i * 20, "completed") for i in range(7)])
        return self.send("<html>x</html>", ctype="text/html")


class WPorg(Base):
    def do_GET(self):
        if self.path.startswith("/core/version-check"):
            return self.send({"offers": [{"current": "6.7.1"}]})
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        slug = q.get("request[slug]", [""])[0]
        if slug == "akismet":
            return self.send({"version": "5.3"})
        if slug == "stary":
            return self.send({"version": "1.0"})
        return self.send({"error": "Plugin not found."}, 404)


class Google(Base):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/token":
            assert b"jwt-bearer" in body
            return self.send({"access_token": "tok", "expires_in": 3600})
        assert self.headers["Authorization"] == "Bearer tok"
        req = json.loads(body)
        if "dimensions" in req:
            return self.send({"rows": [{"keys": ["dentysta wrocław"], "clicks": 40, "position": 3.2}]})
        old = req["startDate"] < "2026-01-01" or Google.prev_marker == req["startDate"]
        Google.prev_marker = Google.prev_marker or req["startDate"]
        clicks = 100 if Google.calls % 2 else 50
        Google.calls += 1
        return self.send({"rows": [{"clicks": clicks, "impressions": 2000, "ctr": 0.03, "position": 8.4}]})

    def do_GET(self):  # PageSpeed
        return self.send({"lighthouseResult": {"categories": {"performance": {"score": 0.42}, "seo": {"score": 0.9},
                                                              "accessibility": {"score": 0.8}},
                                               "audits": {"largest-contentful-paint": {"displayValue": "5,1 s"},
                                                          "cumulative-layout-shift": {"displayValue": "0,02"}}}})


Google.calls = 0
Google.prev_marker = ""


def make_sa():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return json.dumps({"type": "service_account", "client_email": "agent@bsk.iam.gserviceaccount.com", "private_key": pem})


# ------------------------------------------------------------------ SNMP: sztuczny router

def snmp_response(req: bytes, table: dict) -> bytes:
    _, msg, _ = netmon._read(req, 0)
    p = 0
    _, _, p = netmon._read(msg, p)
    _, community, p = netmon._read(msg, p)
    tag, pdu, _ = netmon._read(msg, p)
    q = 0
    _, rid, q = netmon._read(pdu, q)
    _, _, q = netmon._read(pdu, q)
    _, _, q = netmon._read(pdu, q)
    _, vbs, _ = netmon._read(pdu, q)
    oids, r = [], 0
    while r < len(vbs):
        _, vb, r = netmon._read(vbs, r)
        _, o, _ = netmon._read(vb, 0)
        oids.append(netmon._decode_oid(o))
    keys = sorted(table, key=lambda k: [int(x) for x in k.split(".")])
    out = b""
    for oid in oids:
        if tag == 0xA1:
            nxt = next((k for k in keys if [int(x) for x in k.split(".")] > [int(x) for x in oid.split(".")]), None)
            oid, val = (nxt, table[nxt]) if nxt else (oid, None)
        else:
            val = table.get(oid)
        if val is None:
            enc = b"\x82\x00"
        elif isinstance(val, tuple):
            enc = netmon._tlv(val[0], val[1].to_bytes(4, "big"))
        elif isinstance(val, int):
            enc = netmon._int(val)
        else:
            enc = netmon._tlv(0x04, val.encode())
        out += netmon._tlv(0x30, netmon._oid(oid) + enc)
    body = netmon._tlv(0x02, rid) + netmon._int(0) + netmon._int(0) + netmon._tlv(0x30, out)
    return netmon._tlv(0x30, netmon._int(1) + netmon._tlv(0x04, community) + netmon._tlv(0xA2, body))


def fake_router(table):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))

    def run():
        while True:
            try:
                data, addr = s.recvfrom(65535)
            except OSError:
                return
            s.sendto(snmp_response(data, table), addr)
    threading.Thread(target=run, daemon=True).start()
    return s


def test_snmp_roundtrip():
    table = {netmon.SYS_NAME: "Ruijie-Gateway", netmon.SYS_UPTIME: (0x43, 360000), netmon.SYS_DESCR: "EG105GW",
             netmon.IF_DESCR + ".1": "LAN0", netmon.IF_DESCR + ".2": "LAN4", netmon.IF_OPER + ".1": 1, netmon.IF_OPER + ".2": 2,
             "1.3.6.1.2.1.31.1.1.1.1.1": "x"}
    srv = fake_router(table)
    port = srv.getsockname()[1]
    try:
        info = netmon.snmp_get("127.0.0.1", "public", [netmon.SYS_NAME, netmon.SYS_UPTIME], port=port)
        assert info[netmon.SYS_NAME] == "Ruijie-Gateway" and info[netmon.SYS_UPTIME] == 360000
        assert netmon.snmp_walk("127.0.0.1", "public", netmon.IF_OPER, port=port) == {"1": 1, "2": 2}
        st = netmon.device_status("127.0.0.1", "public", port=port)
        assert st["snmp"] and st["name"] == "Ruijie-Gateway" and st["uptime_s"] == 3600
        assert st["ports"] == {"LAN0": "up", "LAN4": "down"} and st["ms"] is not None
    finally:
        srv.close()
    assert netmon.decode_response(netmon.encode_request("public", ["1.3.6.1.2.1.1.5.0"]))[1][0][0] == "1.3.6.1.2.1.1.5.0"


def test_wordpress_and_woocommerce():
    wp, wp_url = serve(WP)
    org, org_url = serve(WPorg)
    try:
        r = wpcheck.check(wp_url + "/", "admin", "abcd efgh", "ck_1", "cs_1", api=org_url)
        assert r["is_wp"] and r["woo"] and r["version"] == "6.4.2" and r["latest"] == "6.7.1" and r["php"] == "7.4.33"
        assert [p["name"] for p in r["outdated"]] == ["Akismet"]
        texts = " ".join(i["text"] for i in r["issues"])
        assert "WordPress 6.4.2 — dostępna wersja 6.7.1" in texts and "Akismet 5.0→5.3" in texts
        assert "PHP 7.4.33" in texts and "Nieudane zamówienia w ciągu doby: 1" in texts
        assert r["orders"]["failed"] == 1 and r["orders"]["day"] == 1
        bad = wpcheck.check(wp_url + "/", "admin", "zle", "ck_1", "zle", api=org_url)
        assert "odrzucił hasło aplikacji" in " ".join(i["text"] for i in bad["issues"])
        assert "odrzucił klucz API" in " ".join(i["text"] for i in bad["issues"])
    finally:
        wp.shutdown()
        org.shutdown()


def test_search_console_and_pagespeed():
    g, g_url = serve(Google)
    try:
        sa = gsc.parse_service_account(make_sa())
        r = gsc.search_console(sa, "sc-domain:klient.pl", token_url=g_url + "/token", base=g_url)
        assert r["current"]["clicks"] == 50 and r["previous"]["clicks"] == 100 and r["change"] == -50
        assert "Spadek kliknięć z Google o 50%" in r["issues"][0]["text"] and r["queries"][0]["query"] == "dentysta wrocław"
        p = gsc.pagespeed("https://klient.pl/", "AIzaTEST", base=g_url + "/psi")
        assert p["performance"] == 42 and p["seo"] == 90 and p["lcp"] == "5,1 s" and p["issues"][0]["level"] == "error"
        with pytest.raises(ValueError):
            gsc.parse_service_account('{"type": "authorized_user"}')
    finally:
        g.shutdown()


def test_panel_settings_integrations_network_report(tmp_path, monkeypatch):
    wp, wp_url = serve(WP)
    org, org_url = serve(WPorg)
    g, g_url = serve(Google)
    router = fake_router({netmon.SYS_NAME: "Brama", netmon.SYS_UPTIME: (0x43, 100000),
                          netmon.IF_DESCR + ".1": "LAN0", netmon.IF_OPER + ".1": 1})
    monkeypatch.setenv("BSK_AGENT_SCHEDULER", "0")
    monkeypatch.setenv("BSK_PSI_URL", g_url + "/psi")
    app = create_app(Settings(data_dir=tmp_path, secret_key="x" * 40, cookie_secure=False, admin_username="admin",
                              admin_password="AdminHaslo123", ai_url="http://127.0.0.1:1"))
    db = app.state.db
    try:
        c = TestClient(app)
        tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
        c.post("/login", data={"username": "admin", "password": "AdminHaslo123", "csrf": tok})
        page = c.get("/admin/agent/settings").text
        assert "Ustawienia i klucze API" in page and "niepodłączone" in page
        tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        # klucze: walidacja, szyfrowanie, maska
        assert "To nie jest poprawny plik JSON" in c.post("/admin/agent/settings", data={"google_sa": "{x", "csrf": tok}).text
        c.post("/admin/agent/settings", data={"google_sa": make_sa(), "psi_key": "AIzaSyTESTKEY1234567890abcd", "csrf": tok})
        raw = monitor.state_get(db, "google_sa")
        assert raw.startswith("enc1:") and "PRIVATE" not in raw and "AIza" not in monitor.state_get(db, "psi_key")
        page = c.get("/admin/agent/settings").text
        assert "agent@bsk.iam.gserviceaccount.com" in page and "abcd" in page and "AIzaSyTESTKEY" not in page
        # strona klienta z WordPress/WooCommerce/Search Console
        c.post("/admin/agent/sites", data={"url": wp_url + "/", "name": "Sklep", "kind": "client", "csrf": tok})
        site = db.one("SELECT * FROM sites")
        c.post(f"/admin/agent/sites/{site['id']}/integrations", data={
            "gsc_property": "sc-domain:sklep.test", "wp_user": "admin", "wp_pass": "abcd efgh", "wc_key": "ck_1",
            "wc_secret": "cs_1", "contact_email": "", "csrf": tok})
        site = db.one("SELECT * FROM sites")
        assert site["wp_pass"].startswith("enc1:") and site["wc_secret"].startswith("enc1:")
        assert SecretBox("x" * 40).open(site["wp_pass"]) == "abcdefgh"  # spacje z hasła aplikacji usunięte
        box = SecretBox("x" * 40)
        integ = integrations.Integrations(db, box, None, wp_api=org_url, token_url=g_url + "/token", gsc_url=g_url,
                                          psi_url=g_url + "/psi")
        db.run("UPDATE sites SET wp_pass = ? WHERE id = ?", box.seal("abcd efgh"), site["id"])
        Google.calls = 0
        res = integ.run_site(db.one("SELECT * FROM sites"))
        texts = " ".join(i["text"] for i in res["issues"])
        assert "Akismet" in texts and "PHP 7.4" in texts and "Spadek kliknięć" in texts and "PageSpeed" in texts
        page = c.get(f"/admin/agent/sites/{site['id']}").text
        assert "Integracje" in page and "WordPress + WooCommerce" in page and "dentysta wrocław" in page
        assert "abcd efgh" not in page and "cs_1" not in page
        # puste pole = klucz bez zmian
        c.post(f"/admin/agent/sites/{site['id']}/integrations", data={"gsc_property": "sc-domain:sklep.test",
                                                                      "wp_user": "admin", "csrf": tok})
        assert box.open(db.one("SELECT wp_pass FROM sites")["wp_pass"]) == "abcd efgh"
        # sieć: urządzenie z SNMP, restart i port, który przestał działać
        c.post("/admin/agent/net", data={"name": "Brama", "host": "127.0.0.1", "community": "public", "csrf": tok})
        assert c.post("/admin/agent/net", data={"name": "x", "host": "zły host!", "csrf": tok}).status_code == 200
        dev = db.one("SELECT * FROM net_devices")
        assert dev["community"].startswith("enc1:")
        port = router.getsockname()[1]
        orig = netmon.device_status
        monkeypatch.setattr(netmon, "device_status", lambda h, c_: orig(h, c_, port=port))
        integ.check_device(db.one("SELECT * FROM net_devices"))
        dev = db.one("SELECT * FROM net_devices")
        assert dev["status"] == "up" and dev["snmp_name"] == "Brama" and json.loads(dev["ports"]) == {"LAN0": "up"}
        db.run("UPDATE net_devices SET uptime_s = 999999, ports = ? WHERE id = ?", json.dumps({"LAN0": "up", "LAN9": "up"}), dev["id"])
        monkeypatch.setattr(netmon, "device_status", lambda h, c_: {**orig(h, c_, port=port), "ports": {"LAN0": "up", "LAN9": "down"}})
        integ.check_device(db.one("SELECT * FROM net_devices"))
        kinds = [e["kind"] for e in db.all("SELECT kind FROM agent_events")]
        assert "net-restart" in kinds and "net-port" in kinds
        alerts = []
        integ.notify_alert = lambda s, b: alerts.append(s)
        monkeypatch.setattr(netmon, "device_status", lambda h, c_: {"ms": None, "snmp": False, "name": "", "uptime_s": None,
                                                                    "ports": {}, "error": ""})
        integ.check_device(db.one("SELECT * FROM net_devices"))
        assert alerts == ["AWARIA sieci: Brama"]
        page = c.get("/admin/agent/siec").text
        assert "Sieć biurowa" in page and "Brama" in page
        # ocena leada bez AI (heurystyka) i z AI (JSON)
        db.run("INSERT INTO sites (url, name, kind, contact_email, phone, source, created_at)"
               " VALUES ('https://firma.test/', 'Firma', 'lead', 'biuro@firma.test', '600', 'dentysta, Wrocław', '2099-01-01')")
        lead = db.one("SELECT * FROM sites WHERE kind = 'lead'")
        score, reason = integ.score_lead(lead, {"score": 40, "facts": {"ttfb_ms": 2000},
                                                "issues": [{"level": "error", "text": "Brak opisu strony."}]})
        assert 60 <= score <= 100 and "Brak opisu" in reason
        monkeypatch.setattr(ai, "generate", lambda cfg, p, s="": 'Oto ocena: {"ocena": 87, "uzasadnienie": "Wolna strona i brak SEO."}')
        integ.ai_cfg = ai.Config(url="http://x")
        assert integ.score_lead(lead, {"score": 40, "facts": {}, "issues": []}) == (87, "Wolna strona i brak SEO.")
        # raport dzienny: kafelki, problemy (sieć, WordPress), leady z oceną
        monkeypatch.setattr(ai, "generate", lambda cfg, p, s="": None)
        rep = report.build(db, None, "https://cloud.test")
        assert "AWARIA" not in rep["subject"] and "pilnych spraw" in rep["subject"]
        assert "Priorytetowe problemy" in rep["html"] and "Sieć: Brama" in rep["html"] and "PHP 7.4" in rep["html"]
        assert "Nowe możliwości sprzedażowe" in rep["html"] and "87/100" in rep["html"] and "Firma" in rep["html"]
        assert "<script" not in rep["html"]
    finally:
        router.close()
        for srv in (wp, org, g):
            srv.shutdown()
