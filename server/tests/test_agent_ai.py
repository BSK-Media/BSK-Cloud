"""Agent AI: monitoring, audyt SEO, gotowe e-maile z adresem, dzienne podsumowanie (sztuczna strona i model)."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app import ai, monitor, review
from app.config import Settings
from app.main import create_app

HOME = """<!doctype html><html><head><title>Sklep</title>
<meta name="viewport" content="width=device-width"></head><body>
<h1>Witamy</h1><h1>Drugi</h1><img src="/a.jpg"><a href="/kontakt">Kontakt</a><a href="/zepsuty">x</a>
<a href="/ok">ok</a></body></html>"""
CONTACT = """<html><body>Napisz: <a href="mailto:biuro@sklep.test">biuro@sklep.test</a> logo@2x.png</body></html>"""


class Site(BaseHTTPRequestHandler):
    down = False

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if Site.down:
            self.send_response(503)
            self.end_headers()
            return
        pages = {"/": HOME, "/kontakt": CONTACT, "/ok": "<html>ok</html>",
                 "/robots.txt": "User-agent: *\nDisallow:\n"}
        body = pages.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain" if self.path.endswith(".txt") else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


class Ollama(BaseHTTPRequestHandler):
    prompts: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._json({"models": [{"name": "qwen3.8:27b"}, {"name": "qwen3:30b"}]})

    bodies: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Ollama.bodies.append(body)
        if body.get("model") == "stary:1b" and "think" in body:
            self.send_response(400)
            self.end_headers()
            return
        Ollama.prompts.append(body["prompt"])
        if "Pierwszy obraz" in body["prompt"]:  # ocena wyglądu ze zrzutów
            text = json.dumps({"wyglad": 3, "nowoczesnosc": 2, "telefon": 4, "marka": 3, "szacowany_rok": "ok. 2012",
                               "przestarzala": True, "rebranding": True, "mocne_strony": ["czytelne menu"],
                               "slabe_strony": ["stare zdjęcia"], "opis": "Strona wygląda na sprzed dekady."})
        elif '"werdykt"' in body["prompt"]:  # pełna ocena i rekomendacja
            text = "<think>analiza</think>" + json.dumps({
                "oceny": {"seo": 4, "wyglad": 2, "nowy_obszar": 9}, "werdykt": "przebudowa",
                "werdykt_uzasadnienie": "Wygląd i kod sprzed lat.", "szacowany_wiek": "ok. 2012",
                "rebranding": True, "uslugi": [{"usluga": "Nowa strona internetowa", "priorytet": "wysoki",
                                                "dlaczego": "Strona jest przestarzała."}],
                "podsumowanie": "Firma potrzebuje nowej strony.", "argumenty": ["Brak opisu strony w Google."],
                "szybkie_poprawki": ["Dodaj opis strony.", "Zostaw jeden nagłówek H1."], "potencjal": 88,
                "potencjal_uzasadnienie": "Duża potrzeba zmian, jest e-mail."})
        elif "Dziennik:" in body["prompt"]:
            text = "Dziś wszystko działa, jedna strona do poprawy."
        elif "e-mail" in body["prompt"]:
            text = "<think>planuję</think>Temat: Kilka poprawek na stronie\n\nDzień dobry,\n\nproponujemy poprawki.\n\nZespół BSK Media"
        else:
            text = "- Dodaj opis strony.\n- Zostaw jeden nagłówek H1."
        self._json({"response": text})

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Shots(BaseHTTPRequestHandler):
    """Udaje kontener „zrzuty” (Gotenberg): zwraca mały obraz JPEG."""
    calls: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        Shots.calls.append((self.path, body))
        data = b"\xff\xd8\xff\xe0fake-jpeg"
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    site, site_url = serve(Site)
    llm, llm_url = serve(Ollama)
    Site.down = False
    Ollama.prompts.clear()
    Ollama.bodies.clear()
    Shots.calls.clear()
    monkeypatch.setenv("BSK_AGENT_SCHEDULER", "0")
    app = create_app(Settings(data_dir=tmp_path, secret_key="x" * 40, cookie_secure=False, admin_username="admin",
                              admin_password="AdminHaslo123", ai_url=llm_url, smtp_host="smtp.test",
                              smtp_from="cloud@bsk.test", notify_email="szef@bsk.test"))
    yield app, site_url, llm_url
    site.shutdown()
    llm.shutdown()


def test_audit_finds_seo_problems(env):
    _, url, _ = env
    r = monitor.audit_site(url + "/")
    texts = " ".join(i["text"] for i in r["issues"])
    assert "Brak opisu strony" in texts and "Kilka nagłówków H1" in texts and "bez opisu alt" in texts
    assert "nie działa przez HTTPS" in texts and "mapy strony" in texts and "/zepsuty (404)" in texts
    assert "Tytuł ma 5 znaków" in texts
    assert 0 <= r["score"] < 70 and r["facts"]["title"] == "Sklep"
    assert monitor.find_emails(CONTACT) == ["biuro@sklep.test"]
    assert monitor.discover_email(url + "/")[0] == "biuro@sklep.test"


def test_agent_flow(env, monkeypatch):
    app, url, _ = env
    sent = []
    monkeypatch.setattr("app.notify.send", lambda settings, to, subject, lines, *a, **k: sent.append((to, subject, lines)))
    monkeypatch.setattr("app.notify.send_html", lambda settings, to, subject, html_body, text, *a, **k: sent.append((to, subject, html_body)))
    c = TestClient(app)
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"username": "admin", "password": "AdminHaslo123", "csrf": tok})
    page = c.get("/admin/agent").text
    assert "Agent AI" in page and "gotowy" in page  # Ollama działa, model pobrany
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post("/admin/agent/sites", data={"url": url + "/", "name": "Sklep ACME", "kind": "lead", "csrf": tok})
    db = app.state.db
    site = db.one("SELECT * FROM sites")
    assert site["kind"] == "lead" and site["url"] == url + "/"
    # monitoring: działa -> awaria (alert) -> powrót (alert)
    alerts = []
    monitor.check_site(db, db.one("SELECT * FROM sites"), lambda s, b: alerts.append(s))
    Site.down = True
    monitor.check_site(db, db.one("SELECT * FROM sites"), lambda s, b: alerts.append(s))
    Site.down = False
    monitor.check_site(db, db.one("SELECT * FROM sites"), lambda s, b: alerts.append(s))
    assert alerts == ["AWARIA strony: Sklep ACME", "Strona działa: Sklep ACME"]
    # audyt + zalecenia AI + gotowy e-mail z adresem znalezionym na stronie
    cfg = ai.Config(url=app.state.settings.ai_url, model="qwen2.5:14b")
    shots_srv, shots_url = serve(Shots)
    review.CONFIG["screenshot_url"] = shots_url
    try:
        monitor.audit_and_store(db, db.one("SELECT * FROM sites"), cfg)
    finally:
        review.CONFIG["screenshot_url"] = ""
        shots_srv.shutdown()
    audit = db.one("SELECT * FROM site_audits")
    assert "Dodaj opis strony" in audit["ai_text"] and "Przebudowa strony" in audit["ai_text"]
    # pełna ocena: zrzuty (komputer + telefon), wygląd oceniony na obrazach, werdykt i usługi
    assert len(Shots.calls) == 2 and Shots.calls[0][0] == "/forms/chromium/screenshot/url" and 'name="clip"' in Shots.calls[0][1]
    vision = next(b for b in Ollama.bodies if "Pierwszy obraz" in b["prompt"])
    assert len(vision["images"]) == 2
    rv = json.loads(audit["review"])
    assert rv["werdykt"] == "przebudowa" and rv["by_ai"] and rv["oceny"]["seo"] == 4 and "nowy_obszar" not in rv["oceny"]
    assert rv["oceny"]["konwersja"] <= 10 and rv["wyglad_ai"]["szacowany_rok"] == "ok. 2012"
    assert rv["potencjal"] == 88
    assert not any("Oceń potencjał" in p for p in Ollama.prompts)  # ocena potencjału bez osobnego zapytania
    site_page = c.get(f"/admin/agent/sites/{site['id']}").text
    assert "Ocena i rekomendacja" in site_page and "Nowa strona internetowa" in site_page and "Wygląd strony" in site_page
    assert "Strona wygląda na sprzed dekady." in site_page and "Sprawdzone podstrony" in site_page
    assert c.get(f"/admin/agent/sites/{site['id']}/zrzut/telefon.jpg").content.startswith(b"\xff\xd8")
    assert c.get(f"/admin/agent/sites/{site['id']}/zrzut/..%2Fx.jpg").status_code == 404
    assert "Przebudowa strony" in c.get("/admin/agent/firmy").text
    d = db.one("SELECT * FROM mail_drafts")
    assert d["to_email"] == "biuro@sklep.test" and "/kontakt" in d["email_source"]
    assert d["subject"] == "Kilka poprawek na stronie" and "<think>" not in d["body"] and d["by_ai"] == 1
    assert any("pierwszy e-mail" in p for p in Ollama.prompts)  # prompt dla potencjalnego klienta
    assert 'href="/admin/agent/maile"' in c.get("/admin/agent").text  # podzakładki w menu bocznym
    page = c.get("/admin/agent/maile").text
    assert "Gotowe e-maile" in page and "biuro@sklep.test" in page and "mailto:biuro@sklep.test?subject=" in page
    assert "napisane przez AI" in page
    assert "Gotowy e-mail" in c.get(f"/admin/agent/sites/{site['id']}").text
    # edycja i oznaczenie jako wysłany
    c.post(f"/admin/agent/drafts/{d['id']}", data={"to_email": "szef@sklep.test", "subject": "Nowy temat",
                                                   "body": "Treść", "action": "save", "csrf": tok})
    assert db.one("SELECT to_email, subject FROM mail_drafts")["subject"] == "Nowy temat"
    assert c.post(f"/admin/agent/drafts/{d['id']}", data={"to_email": "zly", "csrf": tok}).status_code == 200
    assert db.one("SELECT to_email FROM mail_drafts")["to_email"] == "szef@sklep.test"
    # dzienne podsumowanie: AI na górze, awarie, audyt, gotowy e-mail
    subject, lines = monitor.daily_summary(db, cfg)
    text = "\n".join(lines)
    assert lines[0].startswith("Dziś wszystko działa") and "nie działa" in text and "Audyt Sklep ACME" in text
    assert "Do: szef@sklep.test" in text and "Nowy temat" in text
    c.post(f"/admin/agent/summary", data={"csrf": tok})
    import time
    for _ in range(50):
        if sent:
            break
        time.sleep(0.1)
    assert sent and sent[0][0] == "szef@bsk.test" and "Raport AI" in sent[0][1]
    assert "Nowe możliwości sprzedażowe" in sent[0][2] and "Sklep ACME" in sent[0][2] and "Gotowe e-maile" in sent[0][2]
    c.post(f"/admin/agent/drafts/{d['id']}", data={"action": "sent", "csrf": tok})
    assert db.one("SELECT status FROM mail_drafts")["status"] == "sent"
    # bez modelu AI: e-mail z szablonu, agent działa dalej
    monitor.audit_and_store(db, db.one("SELECT * FROM sites"), ai.Config(url="http://127.0.0.1:1", model="x"))
    d2 = db.one("SELECT * FROM mail_drafts WHERE status = 'new'")
    assert d2["by_ai"] == 0 and "Zespół BSK Media" in d2["body"] and d2["to_email"]
    # brak dostępu bez logowania
    assert TestClient(app, follow_redirects=False).get("/admin/agent").status_code == 303


def test_normalize_url():
    assert monitor.normalize_url("klient.pl") == "https://klient.pl/"
    with pytest.raises(ValueError):
        monitor.normalize_url("ftp://x")
    with pytest.raises(ValueError):
        monitor.normalize_url("localhost")


def test_background_tick(env):
    """Jeden obrót agenta: zaległe sprawdzenie, audyt, podsumowanie raz dziennie."""
    app, url, llm = env
    db = app.state.db
    db.run("INSERT INTO sites (url, name, kind, created_at) VALUES (?, 'Klient', 'client', '2026-01-01')", url + "/")
    summaries, alerts = [], []
    agent = monitor.Agent(db, ai.Config(url=llm, model="qwen2.5:14b"), lambda s, b: alerts.append(s),
                          lambda s, lines: summaries.append(s), summary_hour=0)
    agent.tick()
    assert db.one("SELECT COUNT(*) AS n FROM site_checks")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM site_audits")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM mail_drafts WHERE status = 'new'")["n"] == 1
    assert len(summaries) == 1 and not alerts
    agent.tick()  # drugi obrót tego samego dnia: bez nowych sprawdzeń, audytu ani podsumowania
    assert db.one("SELECT COUNT(*) AS n FROM site_checks")["n"] == 1 and len(summaries) == 1


class Overpass(BaseHTTPRequestHandler):
    queries: list = []
    site_url = ""

    def log_message(self, *a):
        pass

    def do_POST(self):
        import urllib.parse as up
        q = up.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())["data"][0]
        Overpass.queries.append(q)
        host = Overpass.site_url.split("//")[1]
        els = [{"tags": {"name": "Gabinet Uśmiech", "website": Overpass.site_url + "/", "email": "rejestracja@usmiech.test",
                         "phone": "+48 600 000 000", "addr:city": "Wrocław"}},
               {"tags": {"name": "Ten sam", "contact:website": "http://" + host + "/"}},  # duplikat domeny
               {"tags": {"name": "Tylko FB", "website": "https://facebook.com/x"}},
               {"tags": {"name": "Bez strony"}}]
        data = json.dumps({"elements": els}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_lead_search_and_panel(tmp_path, monkeypatch):
    from app import leads
    site, site_url = serve(Site)
    llm, llm_url = serve(Ollama)
    osm, osm_url = serve(Overpass)
    Overpass.site_url = site_url
    monkeypatch.setenv("BSK_AGENT_SCHEDULER", "0")
    monkeypatch.setenv("BSK_OVERPASS_URL", osm_url)
    app = create_app(Settings(data_dir=tmp_path, secret_key="x" * 40, cookie_secure=False, admin_username="admin",
                              admin_password="AdminHaslo123", ai_url=llm_url))
    try:
        c = TestClient(app)
        tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
        c.post("/login", data={"username": "admin", "password": "AdminHaslo123", "csrf": tok})
        tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/admin/agent").text).group(1)
        db = app.state.db
        # wyszukiwanie: dentysta, Wrocław -> jedna nowa firma (bez duplikatu domeny, FB i firm bez strony)
        c.post("/admin/agent/searches", data={"category": "dentysta", "city": 'Wrocław"]', "per_day": 5, "csrf": tok})
        assert c.post("/admin/agent/searches", data={"category": "hakerzy", "city": "X", "csrf": tok}).status_code == 200
        assert c.post("/admin/agent/searches", data={"city": "X", "csrf": tok}).status_code == 200  # bez branży
        assert db.one("SELECT COUNT(*) AS n FROM lead_searches")["n"] == 1
        agent = monitor.Agent(db, ai.Config(url=llm_url, model="qwen3:30b"), lambda *a: None, lambda *a: None,
                              summary_hour=25, lead_search=lambda *a: leads.run_search(db, leads.due_search(db), _add(db), osm_url)
                              if leads.due_search(db) else None)
        agent.tick_work()
        q = Overpass.queries[0]
        assert '"amenity"="dentist"' in q and '"name"="Wrocław"' in q and '"]"' not in q.split("area[")[1][:40]
        lead = db.one("SELECT * FROM sites WHERE kind = 'lead'")
        assert lead["name"] == "Gabinet Uśmiech" and lead["contact_email"] == "rejestracja@usmiech.test"
        assert lead["source"] == "dentysta, Wrocław" and db.one("SELECT COUNT(*) AS n FROM sites")["n"] == 1
        # ten sam obrót: audyt nowej firmy i e-mail na adres z OpenStreetMap
        d = db.one("SELECT * FROM mail_drafts")
        assert d["to_email"] == "rejestracja@usmiech.test" and d["email_source"] == "podany ręcznie"
        # qwen3: „myślenie” włączone — jakość ponad szybkość
        assert Ollama.bodies[-1]["think"] is True
        agent.tick_work()  # drugi raz tego samego dnia — bez nowego wyszukiwania
        assert len(Overpass.queries) == 1
        page = c.get("/admin/agent/firmy").text
        assert "Wyszukiwanie nowych firm" in page and "dentysta" in page and "Gabinet Uśmiech" in page
        assert 'data-check-all="category"' in page
        # kilka branż naraz i „Wszystkie branże”
        c.post("/admin/agent/searches", data={"category": ["dentysta", "hotel / pensjonat", "hakerzy"], "city": "Opole", "csrf": tok})
        c.post("/admin/agent/searches", data={"all_categories": "1", "city": "Kraków", "per_day": 10, "csrf": tok})
        rows = db.all("SELECT id, category FROM lead_searches ORDER BY id")
        assert [r["category"] for r in rows[1:]] == ["dentysta|hotel / pensjonat", leads.ALL]
        assert "wszystkie branże" in c.get("/admin/agent/firmy").text
        q = leads.build_query(leads.ALL, "Kraków")
        assert '"amenity"="dentist"' in q and q.count("nwr[") >= len(leads.CATEGORIES)
        db.run("DELETE FROM lead_searches WHERE id > ?", rows[0]["id"])
        # wiele stron naraz
        r = c.post("/admin/agent/sites/bulk", data={"urls": "klient1.pl\nhttps://www.klient2.com.pl/oferta\nzly adres\nklient1.pl",
                                                    "kind": "client", "csrf": tok})
        assert "Dodano stron: 2" in r.text and "zly" in r.text
        # wybór modelu z panelu (zapamiętany)
        c.post("/admin/agent/model/select", data={"model": "gpt-oss:20b", "csrf": tok})
        assert monitor.state_get(db, "ai_model") == "gpt-oss:20b" and "gpt-oss:20b" in c.get("/admin/agent").text
        assert c.post("/admin/agent/model/select", data={"model": "zły model; rm", "csrf": tok}).status_code == 200
        assert monitor.state_get(db, "ai_model") == "gpt-oss:20b"
        # starsza Ollama odrzuca „think” -> ponowienie bez tej opcji
        assert ai.generate(ai.Config(url=llm_url, model="stary:1b"), "Napisz zalecenia") is not None
        assert "think" not in Ollama.bodies[-1]
    finally:
        for srv in (site, llm, osm):
            srv.shutdown()


def _add(db):
    def add(firm, source):
        return db.run("INSERT INTO sites (url, name, kind, contact_email, phone, source, created_at)"
                      " VALUES (?, ?, 'lead', ?, ?, ?, '2026-01-01')", monitor.normalize_url(firm["website"]),
                      firm["name"], firm["email"], firm["phone"], source)
    return add


def test_live_status_card(env):
    """Karta „Co robi agent”: zadania, bieżąca czynność, odświeżany fragment."""
    app, url, llm = env
    db = app.state.db
    c = TestClient(app)
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"username": "admin", "password": "AdminHaslo123", "csrf": tok})
    db.run("INSERT INTO sites (url, name, kind, created_at) VALUES (?, 'Klient', 'client', '2026-01-01')", url + "/")
    live = c.get("/admin/agent/live").text
    assert "<html" not in live and "Czeka na kolejne zadanie" in live and "Pilnuje 1 stron" in live
    assert "1 w kolejce" in live
    # podgląd w trakcie pracy: postęp audytu widoczny z innego wątku
    seen = []
    agent = monitor.Agent(db, ai.Config(url=llm, model="qwen2.5:72b"), lambda *a: None, lambda *a: None, summary_hour=25)
    orig = monitor.ai.generate

    def slow_generate(cfg, prompt, system="", images=None):
        seen.append(agent.overview()["now"][0]["text"])
        return orig(cfg, prompt, system, images)
    monitor.ai.generate = slow_generate
    try:
        agent.tick_work()
    finally:
        monitor.ai.generate = orig
    assert any("przygotowuje pełną ocenę" in t for t in seen) and any("model AI pisze e-mail" in t for t in seen)
    assert agent.overview()["now"] == []
    assert "Co robi agent" in c.get("/admin/agent").text


OLD_SITE = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><head><title>Firma</title><meta name="viewport" content="width=1024">
<script src="/js/jquery-1.8.3.min.js"></script><script>_gaq.push(['_setAccount', 'UA-123456-1']);</script></head>
<body><table><tr><td><font color=red>Witamy</font></td></tr></table><table></table><table></table>
<center>Zapraszamy</center><p>© 2014 Firma</p></body></html>"""

MODERN_SITE = """<!doctype html><html lang="pl"><head><title>Gabinet</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><link rel="icon" href="/f.ico">
<link rel="apple-touch-icon" href="/a.png"><script async src="https://www.googletagmanager.com/gtag/js?id=G-ABC1234"></script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Dentist"}</script></head>
<body><header><nav><a href="/kontakt">Kontakt</a></nav></header><main><h2>Usługi</h2>
<p>""" + "słowo " * 300 + """</p><a href="tel:+48123">Zadzwoń</a><a href="https://facebook.com/x">FB</a>
<form><input type="email"><textarea></textarea></form><a href="/polityka-prywatnosci">Polityka prywatności</a></main>
<footer>© 2020–""" + str(__import__("datetime").date.today().year) + """</footer></body></html>"""


def _scan(html, url="https://firma.test/"):
    issues, facts = [], {}
    review.tech_scan(html, {"X-Powered-By": "PHP/7.2.1"}, url, lambda lvl, text, area: issues.append(
        {"level": lvl, "text": text, "area": area}), facts)
    return issues, facts


def test_review_detects_outdated_site():
    issues, facts = _scan(OLD_SITE)
    text = " ".join(i["text"] for i in issues)
    t = facts["tech"]
    assert t["jquery"] == "1.8.3" and t["copyright_year"] == 2014 and not t["html5"]
    for phrase in ("HTML 4", "<font>", "tabelach", "jQuery 1.8.3", "rok 2014", "stałą szerokością",
                   "Universal Analytics", "PHP 7.2", "telefonu (tel:)", "Mało tekstu"):
        assert phrase in text, phrase
    rv = review.heuristic({"issues": issues, "facts": facts, "score": 20})
    assert rv["werdykt"] == "przebudowa" and rv["uslugi"][0]["usluga"] == "Nowa strona internetowa"
    assert rv["oceny"]["wyglad"] <= 4 and not rv["by_ai"]
    assert "Przebudowa strony" in review.as_text(rv)


def test_review_modern_site_is_clean():
    issues, facts = _scan(MODERN_SITE)
    areas = {i["area"] for i in issues}
    assert "Nowoczesność" not in areas and "Konwersja" not in areas and "Analityka" not in areas, issues
    t = facts["tech"]
    assert t["analytics"] == ["Google Analytics 4"] and t["schema"] == ["Dentist"] and t["social"] == ["Facebook"]
    assert t["phone_link"] and t["form"] and t["words"] >= 300
    # model AI z błędną odpowiedzią -> zostaje ocena automatyczna
    base = review.heuristic({"issues": issues, "facts": facts, "score": 90})
    assert review._clean({"werdykt": "coś innego", "oceny": {"seo": 99}}, base)["oceny"]["seo"] == 10
    assert review._clean({"werdykt": "coś innego"}, base)["werdykt"] == base["werdykt"]
