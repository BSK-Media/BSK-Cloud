"""Agent stron klientów: monitoring działania, audyt SEO i wydajności, codzienne podsumowanie.

Wszystko działa lokalnie na NAS:
- co 15 minut sprawdza, czy strona odpowiada (kod HTTP, czas odpowiedzi, ważność certyfikatu SSL),
- raz w tygodniu robi audyt SEO i podstawowej wydajności (tytuł, opis, nagłówki, alt, robots.txt,
  sitemap, kompresja, waga strony, uszkodzone linki…),
- lokalny model AI (Ollama, moduł ai.py) pisze do audytu zalecenia po polsku,
- raz dziennie wysyła e-mail z podsumowaniem tego, co agent zrobił; awarie zgłasza od razu.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from . import ai, review
from .db import Database, now_iso

log = logging.getLogger("bskcloud.monitor")

CHECK_EVERY_S = 15 * 60
AUDIT_EVERY_DAYS = 7
UA = "Mozilla/5.0 (compatible; BSK-Monitor/1.0; +https://cloud.bskmedia.pl)"
# niektóre serwery i zapory (Cloudflare, Wordfence) odrzucają roboty — ponowna próba jako zwykła przeglądarka
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0 Safari/537.36")
RETRY_DELAYS = (15, 45)  # zanim agent uzna stronę za niedziałającą: dwie kolejne próby po 15 s i 45 s
INTERNET_PROBES = (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443))
MAX_HTML = 5 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    client_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT '',      -- up / down / ''
    status_since TEXT,
    last_check TEXT,
    last_ms INTEGER,
    last_code INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    ssl_expires TEXT,
    kind TEXT NOT NULL DEFAULT 'client',  -- client (strona klienta) / lead (potencjalny klient)
    contact_email TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_checks (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    code INTEGER,
    ms INTEGER,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_site_checks ON site_checks(site_id, ts);
CREATE TABLE IF NOT EXISTS site_audits (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    score INTEGER NOT NULL,
    report TEXT NOT NULL,
    ai_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_site_audits ON site_audits(site_id, ts);
CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    site_id INTEGER,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_events ON agent_events(ts);
CREATE TABLE IF NOT EXISTS agent_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mail_drafts (
    id INTEGER PRIMARY KEY,
    site_id INTEGER REFERENCES sites(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    to_email TEXT NOT NULL DEFAULT '',
    email_source TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    by_ai INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new'  -- new / sent / dismissed
);
"""


def init(db: Database) -> None:
    db.conn().executescript(SCHEMA)
    cols = {r["name"] for r in db.all("PRAGMA table_info(sites)")}
    if "source" not in cols:  # skąd agent ma firmę (np. „dentysta, Wrocław” z wyszukiwania)
        db.run("ALTER TABLE sites ADD COLUMN source TEXT NOT NULL DEFAULT ''")
    if "phone" not in cols:
        db.run("ALTER TABLE sites ADD COLUMN phone TEXT NOT NULL DEFAULT ''")
    if "review" not in {r["name"] for r in db.all("PRAGMA table_info(site_audits)")}:
        # pełna ocena: oceny obszarów, werdykt (przebudowa / odświeżenie …), usługi, ocena wyglądu
        db.run("ALTER TABLE site_audits ADD COLUMN review TEXT NOT NULL DEFAULT ''")


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("Podaj adres strony.")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        raise ValueError("Nieprawidłowy adres strony.")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


def event(db: Database, kind: str, text: str, site_id: int | None = None) -> None:
    db.run("INSERT INTO agent_events (ts, kind, site_id, text) VALUES (?, ?, ?, ?)", now_iso(), kind, site_id, text[:2000])


def state_get(db: Database, key: str, default: str = "") -> str:
    row = db.one("SELECT value FROM agent_state WHERE key = ?", key)
    return row["value"] if row else default


def state_set(db: Database, key: str, value: str) -> None:
    db.run("INSERT INTO agent_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
           key, value)


# ------------------------------------------------------------------ pobieranie stron

class Page:
    def __init__(self, url: str, final_url: str, code: int, headers: dict, body: bytes, ttfb_ms: int, total_ms: int):
        self.url, self.final_url, self.code, self.headers = url, final_url, code, headers
        self.body, self.ttfb_ms, self.total_ms = body, ttfb_ms, total_ms

    @property
    def text(self) -> str:
        charset = "utf-8"
        ctype = self.headers.get("content-type", "")
        if "charset=" in ctype:
            charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def fetch(url: str, limit: int = MAX_HTML, timeout: float = 20, method: str = "GET", ua: str = UA) -> Page:
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Encoding": "identity", "Accept-Language": "pl,en;q=0.8"})
    start = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 — adresy wpisuje administrator
        code = resp.status
    except urllib.error.HTTPError as exc:
        resp, code = exc, exc.code
    ttfb = int((time.monotonic() - start) * 1000)
    try:
        body = resp.read(limit) if method != "HEAD" else b""
    finally:
        resp.close()
    total = int((time.monotonic() - start) * 1000)
    headers = {k.lower(): v for k, v in resp.headers.items()}
    return Page(url, resp.geturl() if hasattr(resp, "geturl") else url, code, headers, body, ttfb, total)


def compressed_size(url: str) -> tuple[str, int | None]:
    """Czy serwer kompresuje HTML (gzip/br) — osobne zapytanie z Accept-Encoding."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, br"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            enc = resp.headers.get("Content-Encoding", "") or ""
            return enc.lower(), len(resp.read(MAX_HTML))
    except (OSError, urllib.error.URLError, ValueError):
        return "", None


def ssl_expiry(url: str, timeout: float = 10) -> datetime | None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return None
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((parts.hostname, parts.port or 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=parts.hostname) as tls:
                cert = tls.getpeercert()
        return datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
    except (OSError, ssl.SSLError, KeyError, ValueError):
        return None


# ------------------------------------------------------------------ monitoring (co 15 minut)

def internet_ok() -> bool:
    """Czy NAS ma w ogóle internet — przy awarii łącza biura strony klientów nie są „nie działa”."""
    for host, port in INTERNET_PROBES:
        try:
            with socket.create_connection((host, port), timeout=5):
                return True
        except OSError:
            continue
    return False


def _probe(url: str, ua: str) -> tuple[bool, int | None, int | None, str]:
    """Jedna próba: (działa, kod HTTP, ms, opis błędu)."""
    try:
        page = fetch(url, limit=65536, timeout=30, ua=ua)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return False, None, None, _err(exc)
    if 200 <= page.code < 400 or page.code == 401:  # 401 = strona chroniona hasłem, serwer działa
        return True, page.code, page.ttfb_ms, ""
    if "cf-mitigated" in page.headers or (page.headers.get("server", "").lower() == "cloudflare"
                                          and page.code in (403, 429, 503)):
        return True, page.code, page.ttfb_ms, ""  # zabezpieczenie przed robotami — dla ludzi strona działa
    return False, page.code, page.ttfb_ms, f"HTTP {page.code}"


def check_site(db: Database, site, notify=None, retry_delays=None) -> dict:
    """Sprawdza stronę, zapisuje wynik, przy zmianie stanu (awaria / powrót) wysyła alert.

    Awaria jest potwierdzana: po nieudanej próbie agent czeka i próbuje jeszcze dwa razy (jako zwykła przeglądarka),
    a przed alarmem sprawdza, czy to nie NAS stracił internet. Chwilowe „czkawki” serwerów nie wywołują alarmów."""
    delays = RETRY_DELAYS if retry_delays is None else retry_delays
    ok, code, ms, error = _probe(site["url"], UA)
    for delay in delays:
        if ok:
            break
        time.sleep(delay)
        ok, code, ms, error = _probe(site["url"], BROWSER_UA)
    if not ok and not internet_ok():
        # problem po stronie NAS / łącza biura — nie zmieniaj stanu strony i nie alarmuj
        db.run("UPDATE sites SET last_check = ?, last_error = ? WHERE id = ?", now_iso(), "brak internetu na NAS", site["id"])
        return {"ok": None, "code": code, "ms": ms, "error": "brak internetu na NAS", "ssl_expires": None}
    expires = ssl_expiry(site["url"]) if ok else None
    now = now_iso()
    db.run("INSERT INTO site_checks (site_id, ts, ok, code, ms, error) VALUES (?, ?, ?, ?, ?, ?)",
           site["id"], now, 1 if ok else 0, code, ms, error)
    new_status = "up" if ok else "down"
    changed = site["status"] and site["status"] != new_status
    db.run("UPDATE sites SET status = ?, status_since = CASE WHEN status = ? THEN status_since ELSE ? END,"
           " last_check = ?, last_ms = ?, last_code = ?, last_error = ?, ssl_expires = COALESCE(?, ssl_expires) WHERE id = ?",
           new_status, new_status, now, now, ms, code, error, expires.isoformat() if expires else None, site["id"])
    if not site["status"] and not ok:
        changed = True  # pierwsze sprawdzenie i od razu awaria
    name = site["name"] or site["url"]
    notify = notify if site["kind"] == "client" else None  # e-maile o awariach tylko dla stron klientów
    if changed:
        if ok:
            event(db, "up", f"{name} znów działa (HTTP {code}, {ms} ms).", site["id"])
            if notify:
                notify(f"Strona działa: {name}", f"{site['url']} znów odpowiada (HTTP {code}, {ms} ms).")
        else:
            event(db, "down", f"{name} nie działa: {error} (potwierdzone {1 + len(delays)} próbami).", site["id"])
            if notify:
                notify(f"AWARIA strony: {name}", f"{site['url']} nie odpowiada poprawnie: {error}.\n\n"
                       f"Potwierdzone {1 + len(delays)} próbami w odstępie ok. minuty, internet na NAS działa.\n"
                       "Agent sprawdza stronę co 15 minut i da znać, gdy wróci.")
    return {"ok": ok, "code": code, "ms": ms, "error": error, "ssl_expires": expires}


def _err(exc: Exception) -> str:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, ssl.SSLError) or isinstance(exc, ssl.SSLError):
        return "błąd certyfikatu SSL"
    if isinstance(reason, socket.gaierror) or isinstance(exc, socket.gaierror):
        return "nie znaleziono domeny (DNS)"
    if isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout)):
        return "brak odpowiedzi (przekroczony czas)"
    if isinstance(reason, ConnectionRefusedError):
        return "serwer odrzuca połączenie"
    return (str(reason or exc) or type(exc).__name__)[:200]


# ------------------------------------------------------------------ audyt SEO i wydajności (raz w tygodniu)

class _Collect(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self._in_title = "", False
        self.meta: dict[str, str] = {}
        self.lang = ""
        self.h1: list[str] = []
        self._in_h1 = False
        self.images = 0
        self.images_no_alt: list[str] = []
        self.links: list[str] = []
        self.canonical = ""
        self.scripts = 0
        self.styles = 0
        self.img_src: list[str] = []
        self.script_src: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "h1":
            self._in_h1 = True
            self.h1.append("")
        elif tag == "img":
            self.images += 1
            if "alt" not in a:
                self.images_no_alt.append(a.get("src", "")[:200])
            if a.get("src"):
                self.img_src.append(a["src"])
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if "canonical" in rel:
                self.canonical = a.get("href", "")
            elif "stylesheet" in rel:
                self.styles += 1
        elif tag == "script" and a.get("src"):
            self.scripts += 1
            self.script_src.append(a["src"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_h1 and self.h1:
            self.h1[-1] += data


def audit_site(url: str, max_links: int = 15) -> dict:
    """Audyt strony głównej: lista problemów (error/warn/ok) i wynik 0–100. Tylko zapytania GET/HEAD."""
    issues: list[dict] = []

    def add(level: str, text: str, area: str):
        issues.append({"level": level, "text": text, "area": area})

    page = fetch(url, timeout=30)
    facts = {"url": url, "final_url": page.final_url, "code": page.code, "ttfb_ms": page.ttfb_ms,
             "total_ms": page.total_ms, "html_kb": round(len(page.body) / 1024)}
    if page.code >= 400:
        add("error", f"Strona zwraca błąd HTTP {page.code}.", "Dostępność")
        return _finish(facts, issues)
    final = urllib.parse.urlsplit(page.final_url)
    if final.scheme != "https":
        add("error", "Strona nie działa przez HTTPS (brak kłódki w przeglądarce, gorsza pozycja w Google).", "Bezpieczeństwo")
    else:
        try:
            plain = fetch(urllib.parse.urlunsplit(("http", final.netloc, "/", "", "")), limit=1024, timeout=15)
            if not plain.final_url.startswith("https://"):
                add("warn", "Wersja http:// nie przekierowuje na https://.", "Bezpieczeństwo")
        except (OSError, urllib.error.URLError, ValueError):
            pass
        exp = ssl_expiry(page.final_url)
        if exp:
            days = (exp - datetime.now(timezone.utc)).days
            facts["ssl_days"] = days
            if days < 14:
                add("error", f"Certyfikat SSL wygasa za {days} dni.", "Bezpieczeństwo")
            elif days < 30:
                add("warn", f"Certyfikat SSL wygasa za {days} dni.", "Bezpieczeństwo")
    # wydajność
    if page.ttfb_ms > 3000:
        add("error", f"Serwer odpowiada bardzo wolno ({page.ttfb_ms} ms do pierwszego bajtu; dobrze < 800 ms).", "Wydajność")
    elif page.ttfb_ms > 1200:
        add("warn", f"Serwer odpowiada wolno ({page.ttfb_ms} ms do pierwszego bajtu; dobrze < 800 ms).", "Wydajność")
    if facts["html_kb"] > 500:
        add("warn", f"Kod HTML strony jest ciężki ({facts['html_kb']} KB).", "Wydajność")
    enc, _ = compressed_size(page.final_url)
    facts["compression"] = enc or "brak"
    if not enc:
        add("warn", "Serwer nie kompresuje strony (gzip/brotli) — wolniejsze ładowanie.", "Wydajność")
    doc = _Collect()
    try:
        doc.feed(page.text)
    except Exception:  # noqa: BLE001 — uszkodzony HTML nie przerywa audytu
        pass
    facts.update({"title": doc.title.strip()[:200], "description": doc.meta.get("description", "")[:300],
                  "h1": [h.strip()[:120] for h in doc.h1][:5], "images": doc.images, "scripts": doc.scripts,
                  "styles": doc.styles, "links": len(doc.links)})
    # podgląd przy udostępnianiu (Open Graph / Twitter) i wynik w Google
    facts["og"] = {k: doc.meta.get(k, "")[:400] for k in ("og:title", "og:description", "og:image", "og:site_name",
                                                         "og:url", "og:type", "twitter:card", "twitter:image")}
    facts["first_image"] = next((urllib.parse.urljoin(page.final_url, src) for src in doc.img_src
                                 if not src.startswith("data:")), "")
    facts["script_srcs"] = [urllib.parse.urljoin(page.final_url, src)[:400] for src in doc.script_src[:40]]
    if doc.scripts > 30:
        add("warn", f"Dużo plików JavaScript ({doc.scripts}) — spowalniają ładowanie.", "Wydajność")
    if doc.styles > 12:
        add("warn", f"Dużo plików CSS ({doc.styles}).", "Wydajność")
    heavy = _heavy_images(page.final_url, doc.img_src[:12])
    if heavy:
        add("warn", f"Ciężkie obrazy (> 400 KB): {', '.join(heavy[:5])}.", "Wydajność")
    # SEO
    title = facts["title"]
    if not title:
        add("error", "Brak tytułu strony (<title>).", "SEO")
    elif not 25 <= len(title) <= 65:
        add("warn", f"Tytuł ma {len(title)} znaków (zalecane 30–60): „{title}”.", "SEO")
    desc = facts["description"]
    if not desc:
        add("error", "Brak opisu strony (meta description) — Google pokaże przypadkowy fragment.", "SEO")
    elif not 70 <= len(desc) <= 165:
        add("warn", f"Opis strony ma {len(desc)} znaków (zalecane 70–160).", "SEO")
    if not doc.h1:
        add("error", "Brak nagłówka H1 na stronie.", "SEO")
    elif len(doc.h1) > 1:
        add("warn", f"Kilka nagłówków H1 ({len(doc.h1)}) — zalecany jeden.", "SEO")
    if "viewport" not in doc.meta:
        add("error", "Brak znacznika viewport — strona może źle wyglądać na telefonach.", "Mobilność")
    if not doc.lang:
        add("warn", "Brak atrybutu lang w <html> (język strony).", "SEO")
    if not doc.canonical:
        add("warn", "Brak adresu kanonicznego (link rel=canonical).", "SEO")
    if not (doc.meta.get("og:title") and doc.meta.get("og:image")):
        add("warn", "Brak znaczników Open Graph (og:title / og:image) — słaby podgląd przy udostępnianiu.", "SEO")
    if doc.images_no_alt:
        add("warn", f"{len(doc.images_no_alt)} z {doc.images} obrazów bez opisu alt.", "SEO")
    if "noindex" in doc.meta.get("robots", "").lower():
        add("error", "Strona ma meta robots „noindex” — Google jej nie zaindeksuje.", "SEO")
    _robots_and_sitemap(page.final_url, add, facts)
    review.tech_scan(page.text, page.headers, page.final_url, add, facts)
    review.crawl_pages(page.final_url, doc.links, add, facts)
    broken = _broken_links(page.final_url, doc.links, max_links)
    if broken:
        add("error", f"Uszkodzone linki ({len(broken)}): " + ", ".join(broken[:5]), "SEO")
    return _finish(facts, issues)


def _finish(facts: dict, issues: list[dict]) -> dict:
    errors = sum(1 for i in issues if i["level"] == "error")
    warns = sum(1 for i in issues if i["level"] == "warn")
    return {"facts": facts, "issues": issues, "score": max(0, 100 - 12 * errors - 4 * warns),
            "errors": errors, "warns": warns}


def _robots_and_sitemap(url: str, add, facts: dict) -> None:
    base = urllib.parse.urlsplit(url)
    root = f"{base.scheme}://{base.netloc}"
    sitemaps: list[str] = []
    try:
        robots = fetch(root + "/robots.txt", limit=200_000, timeout=15)
        if robots.code == 200 and "html" not in robots.headers.get("content-type", ""):
            text = robots.text
            facts["robots"] = True
            block_all, agent_all = False, False
            for line in text.splitlines():
                key, _, val = line.partition(":")
                key, val = key.strip().lower(), val.strip()
                if key == "user-agent":
                    agent_all = val == "*"
                elif key == "disallow" and agent_all and val == "/":
                    block_all = True
                elif key == "sitemap" and val:
                    sitemaps.append(val)
            if block_all:
                add("error", "robots.txt blokuje całą stronę dla wyszukiwarek (Disallow: /).", "SEO")
        else:
            add("warn", "Brak pliku robots.txt.", "SEO")
    except (OSError, urllib.error.URLError, ValueError):
        add("warn", "Nie udało się pobrać robots.txt.", "SEO")
    for sm in sitemaps or [root + "/sitemap.xml", root + "/sitemap_index.xml"]:
        try:
            r = fetch(sm, limit=100_000, timeout=15)
            if r.code == 200 and b"<" in r.body[:200]:
                facts["sitemap"] = sm
                return
        except (OSError, urllib.error.URLError, ValueError):
            continue
    add("warn", "Nie znaleziono mapy strony (sitemap.xml) — utrudnia indeksowanie.", "SEO")


def _broken_links(url: str, links: list[str], limit: int) -> list[str]:
    host = urllib.parse.urlsplit(url).netloc
    seen, broken = set(), []
    for href in links:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urllib.parse.urljoin(url, href).split("#")[0]
        if urllib.parse.urlsplit(full).netloc != host or full in seen:
            continue
        seen.add(full)
        if len(seen) > limit:
            break
        try:
            r = fetch(full, limit=1024, timeout=15)
            if r.code >= 400:
                broken.append(f"{urllib.parse.urlsplit(full).path or '/'} ({r.code})")
        except (OSError, urllib.error.URLError, ValueError):
            broken.append(f"{urllib.parse.urlsplit(full).path or '/'} (brak odpowiedzi)")
    return broken


def _heavy_images(url: str, srcs: list[str]) -> list[str]:
    heavy = []
    for src in srcs:
        if src.startswith("data:"):
            continue
        full = urllib.parse.urljoin(url, src)
        try:
            r = fetch(full, method="HEAD", timeout=10)
            size = int(r.headers.get("content-length", "0") or 0)
            if size > 400 * 1024:
                heavy.append(f"{full.rsplit('/', 1)[-1][:60]} ({size // 1024} KB)")
        except (OSError, urllib.error.URLError, ValueError):
            continue
    return heavy


def _noop(_text: str) -> None:
    pass


def audit_and_store(db: Database, site, ai_cfg: ai.Config | None, progress=_noop, lead_scorer=None) -> int:
    """Pełna ocena strony: audyt techniczny i podstron, zrzuty ekranu, ocena wyglądu przez AI, werdykt i
    rekomendacja usług, potem ocena potencjału (firmy) i gotowy e-mail. Zwraca id audytu."""
    name = site["name"] or site["url"]
    progress(f"Audyt {name}: SEO, szybkość, technologia, podstrony")
    try:
        result = audit_site(site["url"])
    except (OSError, urllib.error.URLError, ValueError) as exc:
        result = _finish({"url": site["url"]}, [{"level": "error", "text": f"Nie udało się pobrać strony: {_err(exc)}.",
                                                 "area": "Dostępność"}])
    reachable = 0 < result["facts"].get("code", 0) < 400
    rv: dict = {}
    if reachable:
        facts = result["facts"]
        review.og_image(site["id"], facts, lambda lvl, text, area: result["issues"].append(
            {"level": lvl, "text": text, "area": area}))
        psi_fn = review.CONFIG.get("pagespeed")
        psi = None
        if psi_fn:
            progress(f"Audyt {name}: test PageSpeed — wydajność i skrypty")
            psi = psi_fn(facts.get("final_url") or site["url"], site["id"])
        if psi and not psi.get("error"):
            facts["psi"] = psi
            result["issues"] += [{**i, "area": "Wydajność"} for i in psi.get("issues", [])]
        else:
            progress(f"Audyt {name}: rozmiary skryptów")
            facts["scripts_static"] = review.script_sizes(facts.get("script_srcs", []), facts.get("final_url") or site["url"])
            if psi and psi.get("error"):
                facts["psi_error"] = psi["error"]
        result.update(_finish(facts, result["issues"]))
        progress(f"Audyt {name}: zrzuty ekranu (komputer i telefon)")
        shots = review.take_screenshots(site["id"], result["facts"].get("final_url") or site["url"])
        result["facts"]["shots"] = sorted(shots)
        visual = None
        if ai_cfg and shots:
            progress(f"Audyt {name}: model AI ocenia wygląd strony")
            visual = review.vision_review(ai_cfg, site["url"], shots)
        result["facts"]["visual"] = bool(visual)
        rv = review.build_review(ai_cfg, site, result, visual, progress)
        result["review"] = rv
    ai_text = review.as_text(rv) if rv.get("by_ai") else ""
    aid = db.run("INSERT INTO site_audits (site_id, ts, score, report, ai_text, review) VALUES (?, ?, ?, ?, ?, ?)",
                 site["id"], now_iso(), result["score"], json.dumps({k: v for k, v in result.items() if k != "review"},
                                                                    ensure_ascii=False),
                 ai_text, json.dumps(rv, ensure_ascii=False) if rv else "")
    verdict = review.VERDICTS.get(rv.get("werdykt"), "")
    event(db, "audit", f"Audyt {name}: {result['score']}/100 ({result['errors']} błędów, {result['warns']} uwag)"
          + (f", werdykt: {verdict}" if verdict else "") + (", ocena AI gotowa" if ai_text else ""), site["id"])
    if reachable:
        if lead_scorer and site["kind"] == "lead":
            lead_scorer(site, result, progress)
        make_draft(db, site, result, ai_text, ai_cfg, progress)
    return aid


# ------------------------------------------------------------------ adres e-mail i gotowe maile

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
_SKIP_EMAIL = ("example.", "sentry", "wixpress", "noreply", "no-reply", "domain.", "email.com", "yourdomain",
               "@2x", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
_CONTACT_WORDS = ("kontakt", "contact", "o-nas", "about", "napisz")


def find_emails(html: str) -> list[str]:
    """Adresy e-mail z linków mailto: i z treści strony (bez obrazków i adresów technicznych)."""
    found = re.findall(r"mailto:([^\"'?>\s]+)", html, flags=re.I) + _EMAIL_RE.findall(html)
    out = []
    for e in found:
        e = urllib.parse.unquote(e).strip().strip(".,;").lower()
        if _EMAIL_RE.fullmatch(e) and not any(x in e for x in _SKIP_EMAIL) and e not in out:
            out.append(e)
    return out[:10]


def discover_email(url: str) -> tuple[str, str]:
    """Adres kontaktowy ze strony głównej albo podstrony kontaktowej: (adres, skąd)."""
    try:
        page = fetch(url, timeout=30)
    except (OSError, urllib.error.URLError, ValueError):
        return "", ""
    host = urllib.parse.urlsplit(page.final_url).hostname or ""
    domain = host.removeprefix("www.")
    emails = find_emails(page.text)
    if not emails:
        doc = _Collect()
        try:
            doc.feed(page.text)
        except Exception:  # noqa: BLE001
            pass
        tried = 0
        for href in doc.links:
            full = urllib.parse.urljoin(page.final_url, href)
            if urllib.parse.urlsplit(full).hostname != host or not any(w in full.lower() for w in _CONTACT_WORDS):
                continue
            tried += 1
            try:
                emails = find_emails(fetch(full, timeout=20).text)
            except (OSError, urllib.error.URLError, ValueError):
                emails = []
            if emails or tried >= 3:
                if emails:
                    url = full
                break
    if not emails:
        return "", ""
    emails.sort(key=lambda e: (not e.endswith("@" + domain), not e.split("@")[0] in ("biuro", "kontakt", "info", "office", "hello")))
    return emails[0], url


DRAFT_PROMPTS = {
    "client": """Jesteś opiekunem klienta w agencji marketingowej BSK Media. Napisz po polsku gotowy e-mail do klienta,
którego stronę {url} prowadzimy. Wynik naszego cotygodniowego audytu: {score}/100.

Wykryte problemy:
{issues}

Ocena i rekomendacja naszego zespołu:
{advice}

E-mail ma: krótko i życzliwie opisać stan strony, wymienić 2–4 najważniejsze sprawy prostym językiem (bez żargonu),
zaproponować, że BSK Media je poprawi (a jeśli rekomendacja to przebudowa lub odświeżenie wyglądu — delikatnie
zaproponować także to), i zaprosić do kontaktu. Ton: profesjonalny, ciepły, na „Pan/Pani”
bez imienia (formuła „Dzień dobry,”). Podpis: „Zespół BSK Media”. Długość do 180 słów.
Pierwsza linia odpowiedzi to temat w formacie „Temat: …”, potem pusta linia i treść.""",
    "lead": """Jesteś specjalistą ds. nowych klientów w agencji marketingowej BSK Media (strony WWW, SEO, marketing).
Przeanalizowaliśmy publiczną stronę firmy {url}. Wynik audytu: {score}/100.

Wykryte problemy:
{issues}

Nasza wewnętrzna ocena (nie cytuj jej dosłownie, nie podawaj ocen liczbowych):
{advice}

Napisz po polsku krótki, uprzejmy pierwszy e-mail do tej firmy: przedstaw BSK Media jednym zdaniem, podaj 2–3
konkretne obserwacje z audytu (bez straszenia i bez przesady) i zaproponuj to, co ocena wskazuje jako najważniejsze
(np. nową stronę, odświeżenie wyglądu albo poprawę widoczności w Google), z bezpłatną rozmową lub raportem.
Zapytaj, czy możemy przesłać szczegóły — bez nachalnej sprzedaży. Formuła „Dzień dobry,”, podpis
„Zespół BSK Media”. Do 150 słów. Pierwsza linia odpowiedzi to temat w formacie „Temat: …”,
potem pusta linia i treść.""",
}


def _split_subject(text: str, fallback: str) -> tuple[str, str]:
    lines = text.strip().splitlines()
    if lines and lines[0].lower().startswith("temat:"):
        return lines[0].split(":", 1)[1].strip().strip("„”\"")[:200] or fallback, "\n".join(lines[1:]).strip()
    return fallback, text.strip()


def _template_draft(site, result: dict) -> tuple[str, str]:
    """Gotowy mail bez AI (gdy model jest niedostępny) — z najważniejszych problemów audytu."""
    top = [i["text"] for i in sorted(result["issues"], key=lambda i: i["level"] != "error")][:4]
    points = "\n".join(f"- {t}" for t in top)
    if site["kind"] == "lead":
        subject = f"Kilka obserwacji o stronie {urllib.parse.urlsplit(site['url']).hostname}"
        body = ("Dzień dobry,\n\nnazywamy się BSK Media — tworzymy i rozwijamy strony internetowe. Przy okazji przeglądu "
                f"lokalnych firm zajrzeliśmy na stronę {site['url']} i zauważyliśmy kilka rzeczy, które mogą wpływać na "
                f"pozycję w Google i wygodę klientów:\n\n{points}\n\nChętnie przygotujemy bezpłatny, krótki raport z "
                "propozycją poprawek. Czy możemy go przesłać?\n\nPozdrawiamy,\nZespół BSK Media")
    else:
        subject = f"Przegląd strony {urllib.parse.urlsplit(site['url']).hostname} — {result['score']}/100"
        body = (f"Dzień dobry,\n\nw ramach stałej opieki sprawdziliśmy stronę {site['url']}. Wynik przeglądu to "
                f"{result['score']}/100. Najważniejsze sprawy do poprawy:\n\n{points}\n\nChętnie zajmiemy się "
                "poprawkami — prosimy o informację, czy możemy zacząć.\n\nPozdrawiamy,\nZespół BSK Media")
    return subject, body


def make_draft(db: Database, site, result: dict, ai_text: str, ai_cfg: ai.Config | None, progress=_noop) -> int | None:
    """Gotowy e-mail do klienta / potencjalnego klienta z adresem odbiorcy. Nic nie jest wysyłane."""
    if not result["issues"]:
        return None
    to, source = site["contact_email"], "podany ręcznie" if site["contact_email"] else ""
    if not to and site["client_id"]:
        row = db.one("SELECT email FROM users WHERE id = ?", site["client_id"])
        if row and row["email"]:
            to, source = row["email"], "konto klienta w portalu"
    if not to:
        progress(f"{site['name'] or site['url']}: szukam adresu e-mail na stronie")
        to, where = discover_email(site["url"])
        source = f"znaleziony na {where}" if to else ""
    subject, body, by_ai = "", "", 0
    if ai_cfg:
        prompt = DRAFT_PROMPTS["lead" if site["kind"] == "lead" else "client"].format(
            url=site["url"], score=result["score"], advice=ai_text or (review.as_text(result["review"]) if result.get("review") else "(brak)"),
            issues="\n".join(f"- {i['text']}" for i in result["issues"][:10]))
        progress(f"{site['name'] or site['url']}: model AI pisze e-mail")
        text = ai.generate(ai_cfg, prompt)
        if text:
            subject, body = _split_subject(text, f"Strona {urllib.parse.urlsplit(site['url']).hostname}")
            by_ai = 1
    if not body:
        subject, body = _template_draft(site, result)
    db.run("UPDATE mail_drafts SET status = 'dismissed' WHERE site_id = ? AND status = 'new'", site["id"])
    did = db.run("INSERT INTO mail_drafts (site_id, ts, to_email, email_source, subject, body, by_ai) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 site["id"], now_iso(), to, source, subject, body, by_ai)
    name = site["name"] or site["url"]
    event(db, "mail", f"Gotowy e-mail do {to or '(brak adresu — uzupełnij)'} w sprawie {name}.", site["id"])
    return did


# ------------------------------------------------------------------ codzienne podsumowanie

SUMMARY_PROMPT = """Jesteś asystentem agencji BSK Media. Na podstawie dziennika pracy agenta z ostatnich 24 godzin
napisz po polsku krótkie (3–6 zdań) podsumowanie dla właściciela agencji: co jest najważniejsze, co wymaga
działania dziś, co działa dobrze. Konkretnie, bez wstępów i bez wymyślania faktów spoza dziennika.

Dziennik:
{log}"""


def daily_summary(db: Database, ai_cfg: ai.Config | None, progress=_noop) -> tuple[str, list[str]]:
    """Temat i akapity e-maila z podsumowaniem ostatnich 24 godzin."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    events = db.all("SELECT * FROM agent_events WHERE ts > ? ORDER BY ts", since)
    sites = db.all("SELECT * FROM sites WHERE active = 1 ORDER BY name, url")
    checks = db.one("SELECT COUNT(*) AS n, SUM(ok) AS ok FROM site_checks WHERE ts > ?", since)
    down = [s for s in sites if s["status"] == "down"]
    lines = []
    status_line = (f"Monitorowane strony: {len(sites)}. Sprawdzeń w ciągu doby: {checks['n'] or 0}"
                   + (f", w tym nieudanych: {(checks['n'] or 0) - (checks['ok'] or 0)}." if checks["n"] else "."))
    if down:
        status_line += " Teraz nie działają: " + ", ".join(s["name"] or s["url"] for s in down) + "."
    soon = []
    for s in sites:
        if s["ssl_expires"]:
            try:
                days = (datetime.fromisoformat(s["ssl_expires"]) - datetime.now(timezone.utc)).days
            except ValueError:
                continue
            if days < 21:
                soon.append(f"{s['name'] or s['url']} ({days} dni)")
    if soon:
        status_line += " Certyfikaty SSL wkrótce wygasają: " + ", ".join(soon) + "."
    log_lines = [f"{_hhmm(e['ts'])} {e['text']}" for e in events]
    audits = db.all("SELECT a.*, s.name, s.url FROM site_audits a JOIN sites s ON s.id = a.site_id WHERE a.ts > ?"
                    " ORDER BY a.ts", since)
    intro = ""
    if ai_cfg and (log_lines or down or soon):
        progress("Model AI pisze dzienne podsumowanie")
        intro = ai.generate(ai_cfg, SUMMARY_PROMPT.format(log="\n".join([status_line] + log_lines[-60:]))) or ""
    if intro:
        lines.append(intro.strip())
    lines.append(status_line)
    for a in audits:
        rep = json.loads(a["report"])
        top = [i["text"] for i in rep.get("issues", []) if i["level"] == "error"][:3]
        lines.append(f"Audyt {a['name'] or a['url']}: {a['score']}/100." + (" Najważniejsze: " + " ".join(top) if top else ""))
        if a["ai_text"]:
            lines.append("Zalecenia AI:\n" + a["ai_text"].strip())
    drafts = db.all("SELECT d.*, s.name, s.url FROM mail_drafts d JOIN sites s ON s.id = d.site_id"
                    " WHERE d.status = 'new' ORDER BY d.ts")
    if drafts:
        lines.append(f"Gotowe e-maile do wysłania ({len(drafts)}) — przejrzyj je w panelu Agent AI i wyślij jednym kliknięciem:")
        for d in drafts[-10:]:
            lines.append(f"Do: {d['to_email'] or '(brak adresu)'}\nTemat: {d['subject']}\n\n{d['body']}")
    if log_lines:
        lines.append("Dziennik agenta:\n" + "\n".join(log_lines[-40:]))
    else:
        lines.append("W ciągu ostatniej doby nie było awarii ani nowych audytów — wszystko działa.")
    subject = (f"Agent: {len(down)} stron nie działa" if down else "Agent: dzienne podsumowanie") \
        + f" ({datetime.now().strftime('%d.%m')})"
    return subject, lines


def _hhmm(iso: str) -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(iso).astimezone(ZoneInfo("Europe/Warsaw")).strftime("%d.%m %H:%M")
    except Exception:  # noqa: BLE001
        return iso[5:16].replace("T", " ")


# ------------------------------------------------------------------ praca w tle

class Agent:
    """Dwie pętle w tle: szybka (sprawdzanie stron co 15 min, nie czeka na AI) i robocza
    (wyszukiwanie firm, audyty z AI, gotowe e-maile, dzienne podsumowanie)."""

    def __init__(self, db: Database, ai_cfg: ai.Config | None, notify_alert, send_summary, summary_hour: int = 7,
                 lead_search=None, integrations=None, build_report=None):
        self.db, self.ai_cfg = db, ai_cfg
        self.notify_alert, self.send_summary = notify_alert, send_summary
        self.summary_hour = summary_hour
        self.lead_search = lead_search  # funkcja: jedno zaległe wyszukiwanie firm (albo None)
        self.integrations = integrations  # WordPress/WooCommerce, Google, sieć, ocena leadów (albo None)
        self.build_report = build_report  # raport HTML (albo None = tekst)
        self.lock = threading.Lock()
        self.now: dict[str, dict | None] = {"work": None, "checks": None}  # podgląd w panelu: co robi teraz
        self.started_at = now_iso()

    @property
    def busy(self) -> str:
        return (self.now["work"] or {}).get("text", "")

    def _set(self, kind: str, text: str | None) -> None:
        if text is None:
            self.now[kind] = None
        elif not self.now[kind] or self.now[kind]["text"] != text:
            self.now[kind] = {"text": text, "since": now_iso()}

    def progress(self, text: str) -> None:
        self._set("work", text)

    def tick(self) -> None:
        self.tick_checks()
        self.tick_work()

    def tick_checks(self) -> None:
        """Zaległe sprawdzenia działania stron."""
        due = datetime.now(timezone.utc) - timedelta(seconds=CHECK_EVERY_S)
        try:
            for site in self.db.all("SELECT * FROM sites WHERE active = 1 AND (last_check IS NULL OR last_check < ?)",
                                    due.isoformat()):
                self._set("checks", f"Sprawdzam, czy działa {site['name'] or site['url']}")
                check_site(self.db, site, self.notify_alert)
            if self.integrations:
                self.integrations.net_tick(lambda t: self._set("checks", t))
        finally:
            self._set("checks", None)

    def tick_work(self) -> None:
        """Wyszukiwanie firm (raz na dobę), najwyżej jeden audyt, podsumowanie o ustalonej godzinie."""
        with self.lock:
            try:
                if self.lead_search:
                    self.lead_search(self.progress)
                old = (datetime.now(timezone.utc) - timedelta(days=AUDIT_EVERY_DAYS)).isoformat()
                site = self.db.one(
                    "SELECT s.* FROM sites s WHERE s.active = 1 AND NOT EXISTS (SELECT 1 FROM site_audits a"
                    " WHERE a.site_id = s.id AND a.ts > ?) ORDER BY EXISTS (SELECT 1 FROM site_audits b WHERE b.site_id = s.id),"
                    " s.kind = 'lead', s.id LIMIT 1", old)
                if site:
                    audit_and_store(self.db, site, self.ai_cfg, self.progress,
                                    self.integrations.score_lead if self.integrations else None)
                if self.integrations:
                    due = self.integrations.due_site()
                    if due:
                        self.integrations.run_site(due, self.progress)
                self._maybe_summary()
            finally:
                self._set("work", None)

    def _maybe_summary(self) -> None:
        try:
            from zoneinfo import ZoneInfo
            local = datetime.now(ZoneInfo("Europe/Warsaw"))
        except Exception:  # noqa: BLE001
            local = datetime.now()
        today = local.date().isoformat()
        if local.hour >= self.summary_hour and state_get(self.db, "summary_date") != today:
            state_set(self.db, "summary_date", today)
            self.summary_now()

    def summary_now(self) -> None:
        self.progress("Przygotowuję dzienne podsumowanie")
        try:
            if self.build_report:
                rep_ = self.build_report(self.progress)
                self.send_summary(rep_["subject"], rep_["lines"], rep_["html"])
            else:
                subject, lines = daily_summary(self.db, self.ai_cfg, self.progress)
                self.send_summary(subject, lines)
            state_set(self.db, "summary_sent_at", now_iso())
        finally:
            self._set("work", None)

    def overview(self) -> dict:
        """Zadania agenta i ich stan — karta „Co robi agent” w panelu."""
        db = self.db
        sites = db.one("SELECT COUNT(*) AS n, SUM(status = 'down' AND kind = 'client') AS down, SUM(kind = 'lead') AS leads"
                       " FROM sites WHERE active = 1")
        old = (datetime.now(timezone.utc) - timedelta(days=AUDIT_EVERY_DAYS)).isoformat()
        queue = db.one("SELECT COUNT(*) AS n FROM sites s WHERE s.active = 1 AND NOT EXISTS (SELECT 1 FROM site_audits a"
                       " WHERE a.site_id = s.id AND a.ts > ?)", old)["n"]
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        day = db.one("SELECT (SELECT COUNT(*) FROM site_checks WHERE ts > ?) AS checks,"
                     " (SELECT COUNT(*) FROM site_audits WHERE ts > ?) AS audits,"
                     " (SELECT COUNT(*) FROM mail_drafts WHERE ts > ?) AS mails,"
                     " (SELECT COUNT(*) FROM agent_events WHERE kind = 'lead' AND ts > ?) AS leads",
                     since, since, since, since)
        searches = db.all("SELECT category, city, per_day, active FROM lead_searches") if db.one(
            "SELECT 1 FROM sqlite_master WHERE name = 'lead_searches'") else []
        return {
            "now": [x for x in (self.now["work"], self.now["checks"]) if x],
            "sites": sites["n"] or 0, "down": sites["down"] or 0, "leads": sites["leads"] or 0,
            "audit_queue": queue, "day": dict(day),
            "drafts": db.one("SELECT COUNT(*) AS n FROM mail_drafts WHERE status = 'new'")["n"],
            "searches": [dict(r) for r in searches if r["active"]],
            "summary_hour": self.summary_hour, "summary_sent": state_get(db, "summary_sent_at"),
            "recent": [dict(r) for r in db.all("SELECT ts, kind, text FROM agent_events ORDER BY ts DESC LIMIT 6")],
        }

    def start(self) -> None:
        def loop(fn, name):
            time.sleep(60)
            while True:
                try:
                    fn()
                except Exception:  # noqa: BLE001 — agent nie może zatrzymać portalu
                    log.exception("Błąd agenta (%s)", name)
                time.sleep(60)
        threading.Thread(target=loop, args=(self.tick_checks, "monitoring"), name="agent-monitoring", daemon=True).start()
        threading.Thread(target=loop, args=(self.tick_work, "praca"), name="agent-praca", daemon=True).start()
