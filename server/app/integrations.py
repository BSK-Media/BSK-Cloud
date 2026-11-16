"""Integracje agenta: WordPress/WooCommerce, Search Console, PageSpeed (raz na dobę na stronę),
sieć biurowa (co 5 minut) i ocena potencjału sprzedażowego firm (po audycie)."""
from __future__ import annotations

import json
import logging
import re
import urllib.error
from datetime import datetime, timedelta, timezone

from . import ai, gsc, netmon, review, speed, wpcheck
from .db import Database, now_iso
from .monitor import event, state_get
from .secretbox import SecretBox

log = logging.getLogger("bskcloud.integrations")
INTEG_EVERY_H = 24
NET_EVERY_S = 5 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS site_staff (           -- opiekunowie stron klientów (pracownicy z dostępem do raportu)
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (site_id, user_id)
);
CREATE TABLE IF NOT EXISTS net_devices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    community TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    last_check TEXT,
    last_ms REAL,
    snmp_name TEXT NOT NULL DEFAULT '',
    uptime_s INTEGER,
    ports TEXT NOT NULL DEFAULT '{}',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""
SITE_COLUMNS = [
    ("gsc_property", "TEXT NOT NULL DEFAULT ''"), ("wp_user", "TEXT NOT NULL DEFAULT ''"),
    ("wp_pass", "TEXT NOT NULL DEFAULT ''"), ("wc_key", "TEXT NOT NULL DEFAULT ''"),
    ("wc_secret", "TEXT NOT NULL DEFAULT ''"), ("integ", "TEXT NOT NULL DEFAULT '{}'"), ("integ_at", "TEXT"),
    ("lead_score", "INTEGER"), ("lead_reason", "TEXT NOT NULL DEFAULT ''"),
    ("ga_property", "TEXT NOT NULL DEFAULT ''"),  # identyfikator usługi Google Analytics 4 (same cyfry)
]


def init(db: Database) -> None:
    db.conn().executescript(SCHEMA)
    cols = {r["name"] for r in db.all("PRAGMA table_info(sites)")}
    for name, ddl in SITE_COLUMNS:
        if name not in cols:
            db.run(f"ALTER TABLE sites ADD COLUMN {name} {ddl}")


class Integrations:
    def __init__(self, db: Database, box: SecretBox, ai_cfg: ai.Config | None, notify_alert=None,
                 wp_api: str = wpcheck.WP_API, token_url: str = gsc.TOKEN_URL, gsc_url: str = gsc.GSC_URL,
                 psi_url: str = gsc.PSI_URL, ga_url: str = gsc.GA_URL):
        self.db, self.box, self.ai_cfg, self.notify_alert = db, box, ai_cfg, notify_alert
        self.wp_api, self.token_url, self.gsc_url, self.psi_url = wp_api, token_url, gsc_url, psi_url
        self.ga_url = ga_url

    def pagespeed(self, url: str, site_id: int | None = None, strategy: str = "mobile") -> dict | None:
        """Test PageSpeed (z klucza w ustawieniach), zapisany w historii strony. None = brak klucza."""
        key = self.psi_key()
        if not key:
            return None
        try:
            full = speed.parse(speed.fetch(url, key, strategy, self.psi_url), url)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200], "issues": []}
        if site_id:
            speed.store(self.db, site_id, strategy, full)
        out = speed.compact(full)
        if strategy == "desktop":
            out["issues"] = speed.issues(full, "komputer")
        return out

    # --- klucze z ustawień
    def google_account(self) -> dict | None:
        raw = self.box.open(state_get(self.db, "google_sa"))
        if not raw:
            return None
        try:
            return gsc.parse_service_account(raw)
        except ValueError:
            return None

    def psi_key(self) -> str:
        return self.box.open(state_get(self.db, "psi_key"))

    # --- strona: WordPress, WooCommerce, Search Console, PageSpeed
    def run_site(self, site, progress=lambda t: None) -> dict:
        name = site["name"] or site["url"]
        result: dict = {"issues": []}
        progress(f"{name}: sprawdzam WordPress i WooCommerce")
        try:
            wp = wpcheck.check(site["url"], site["wp_user"], self.box.open(site["wp_pass"]),
                               self.box.open(site["wc_key"]), self.box.open(site["wc_secret"]), api=self.wp_api)
            if wp["is_wp"]:
                result["wp"] = wp
                result["issues"] += [{**i, "area": "WordPress"} for i in wp["issues"]]
        except Exception as exc:  # noqa: BLE001 — jedna integracja nie zatrzymuje pozostałych
            log.warning("WordPress %s: %s", site["url"], exc)
        sa = self.google_account()
        if sa and site["gsc_property"]:
            progress(f"{name}: pobieram dane z Google Search Console")
            try:
                result["gsc"] = gsc.search_console(sa, site["gsc_property"], token_url=self.token_url, base=self.gsc_url)
                result["issues"] += [{**i, "area": "Google"} for i in result["gsc"]["issues"]]
            except urllib.error.HTTPError as exc:
                msg = "brak dostępu — dodaj konto usługi w Search Console klienta" if exc.code == 403 else f"HTTP {exc.code}"
                result["gsc"] = {"error": msg}
                result["issues"].append({"level": "warn", "area": "Google", "text": f"Search Console: {msg}."})
            except Exception as exc:  # noqa: BLE001
                result["gsc"] = {"error": str(exc)[:200]}
        if sa and site["ga_property"]:
            progress(f"{name}: pobieram dane z Google Analytics")
            try:
                result["ga"] = gsc.analytics(sa, site["ga_property"], token_url=self.token_url, base=self.ga_url)
                result["issues"] += [{**i, "area": "Google Analytics"} for i in result["ga"]["issues"]]
            except urllib.error.HTTPError as exc:
                msg = ("brak dostępu — dodaj konto usługi w Google Analytics klienta (rola „Wyświetlający”) i włącz "
                       "Google Analytics Data API w Google Cloud") if exc.code in (403, 401) else f"HTTP {exc.code}"
                result["ga"] = {"error": msg}
                result["issues"].append({"level": "warn", "area": "Google Analytics", "text": f"Google Analytics: {msg}."})
            except Exception as exc:  # noqa: BLE001
                result["ga"] = {"error": str(exc)[:200]}
        if self.psi_key() and site["kind"] == "client":
            progress(f"{name}: test PageSpeed Insights (telefon)")
            result["psi"] = self.pagespeed(site["url"], site["id"], "mobile")
            result["issues"] += [{**i, "area": "PageSpeed"} for i in result["psi"].get("issues", [])]
            progress(f"{name}: test PageSpeed Insights (komputer)")
            desk = self.pagespeed(site["url"], site["id"], "desktop")
            if desk and not desk.get("error"):
                result["psi_desktop"] = {k: desk.get(k) for k in ("performance", "lcp", "tbt", "cls")}
        old = json.loads(site["integ"] or "{}")
        self.db.run("UPDATE sites SET integ = ?, integ_at = ? WHERE id = ?", json.dumps(result, ensure_ascii=False),
                    now_iso(), site["id"])
        new_errors = [i for i in result["issues"] if i["level"] == "error"]
        old_texts = {i["text"] for i in old.get("issues", [])}
        for i in new_errors:
            if i["text"] not in old_texts:
                event(self.db, "problem", f"{name}: {i['text']}", site["id"])
        if result.get("wp") or result.get("gsc") or result.get("ga") or result.get("psi"):
            parts = []
            if result.get("wp"):
                parts.append("WordPress" + (" + WooCommerce" if result["wp"].get("orders") else ""))
            if result.get("gsc") and "error" not in result["gsc"]:
                parts.append("Search Console")
            if result.get("ga") and "error" not in result["ga"]:
                parts.append("Google Analytics")
            if result.get("psi") and "error" not in result["psi"]:
                parts.append("PageSpeed")
            event(self.db, "integ", f"{name}: sprawdzono {', '.join(parts)} — {len(result['issues'])} uwag.", site["id"])
        return result

    def due_site(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=INTEG_EVERY_H)).isoformat()
        return self.db.one("SELECT * FROM sites WHERE active = 1 AND kind = 'client' AND (integ_at IS NULL OR integ_at < ?)"
                           " ORDER BY integ_at IS NOT NULL, integ_at LIMIT 1", old)

    # --- sieć biurowa
    def net_tick(self, progress=lambda t: None) -> None:
        due = (datetime.now(timezone.utc) - timedelta(seconds=NET_EVERY_S)).isoformat()
        for d in self.db.all("SELECT * FROM net_devices WHERE last_check IS NULL OR last_check < ?", due):
            progress(f"Sieć: sprawdzam {d['name']}")
            self.check_device(d)

    def check_device(self, d) -> dict:
        st = netmon.device_status(d["host"], self.box.open(d["community"]))
        ok = st["ms"] is not None
        status = "up" if ok else "down"
        if d["status"] and d["status"] != status:
            text = f"{d['name']} ({d['host']}) " + ("znów odpowiada." if ok else "nie odpowiada!")
            event(self.db, "net-up" if ok else "net-down", text)
            if self.notify_alert:
                self.notify_alert(("Sieć: " if ok else "AWARIA sieci: ") + d["name"], text)
        if ok and st["uptime_s"] is not None and d["uptime_s"] and st["uptime_s"] + 600 < d["uptime_s"]:
            event(self.db, "net-restart", f"{d['name']} uruchomił się ponownie (pracuje od {st['uptime_s'] // 60} min).")
        old_ports = json.loads(d["ports"] or "{}")
        went_down = [p for p, s in st["ports"].items() if s == "down" and old_ports.get(p) == "up"]
        if went_down:
            event(self.db, "net-port", f"{d['name']}: porty przestały działać: {', '.join(went_down[:8])}.")
        self.db.run("UPDATE net_devices SET status = ?, last_check = ?, last_ms = ?, snmp_name = ?, uptime_s = ?,"
                    " ports = ?, last_error = ? WHERE id = ?", status, now_iso(), st["ms"], st["name"] or d["snmp_name"],
                    st["uptime_s"], json.dumps(st["ports"] or old_ports), st["error"], d["id"])
        return st

    # --- ocena potencjału sprzedażowego firmy (po audycie)
    def score_lead(self, site, result: dict, progress=lambda t: None) -> tuple[int, str]:
        f = result.get("facts", {})
        problems = [i for i in result.get("issues", [])]
        heur = min(100, max(0, round((100 - result.get("score", 100)) * 0.8 + (12 if site["contact_email"] else 0)
                                     + (5 if site["phone"] else 0) + (8 if f.get("ttfb_ms", 0) > 1200 else 0))))
        reason = ""
        rv = result.get("review") or {}
        if rv.get("by_ai") and rv.get("potencjal") is not None:  # pełna ocena AI już to policzyła
            heur = int(rv["potencjal"])
            verdict = review.VERDICTS.get(rv.get("werdykt"), "")
            reason = (f"{verdict}. " if verdict else "") + (rv.get("potencjal_uzasadnienie") or rv.get("podsumowanie", ""))[:600]
        elif rv:
            heur = round((heur + int(rv.get("potencjal", heur))) / 2)
        if self.ai_cfg and not reason:
            progress(f"{site['name'] or site['url']}: model AI ocenia potencjał sprzedażowy")
            prompt = LEAD_PROMPT.format(
                url=site["url"], name=site["name"] or "", source=site["source"] or "", score=result.get("score", "?"),
                issues="\n".join(f"- {i['text']}" for i in problems[:12]) or "- brak istotnych problemów",
                email="tak" if site["contact_email"] else "nie", title=f.get("title", ""))
            text = ai.generate(self.ai_cfg, prompt) or ""
            m = re.search(r"\{.*\}", text, flags=re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                    heur = max(0, min(100, int(data.get("ocena", heur))))
                    reason = str(data.get("uzasadnienie", ""))[:600]
                except (ValueError, TypeError):
                    pass
        if not reason:
            top = [i["text"] for i in problems if i["level"] == "error"][:2] or [i["text"] for i in problems][:2]
            verdict = review.VERDICTS.get(rv.get("werdykt"), "")
            reason = (f"{verdict}. " if verdict else "") + ("Strona ma problemy, które BSK Media może naprawić: " + " ".join(top)) if top else \
                "Strona w dobrym stanie — mniejsza potrzeba zmian."
        self.db.run("UPDATE sites SET lead_score = ?, lead_reason = ? WHERE id = ?", heur, reason, site["id"])
        return heur, reason


LEAD_PROMPT = """Jesteś handlowcem agencji BSK Media (strony WWW, sklepy internetowe, SEO, marketing). Oceń potencjał
sprzedażowy firmy na podstawie audytu jej strony: im więcej problemów, które możemy naprawić, i im łatwiejszy
kontakt, tym wyższa ocena. Firma: {name} ({source}), strona {url}, tytuł strony „{title}”, wynik audytu {score}/100,
znaleziony e-mail: {email}.

Problemy:
{issues}

Odpowiedz wyłącznie obiektem JSON: {{"ocena": liczba 0-100, "uzasadnienie": "1-2 zdania po polsku: dlaczego
i od czego zacząć rozmowę"}}"""


def site_view(site, box: SecretBox) -> dict:
    """Dane strony do panelu: klucze tylko jako maska."""
    from .secretbox import mask
    d = dict(site)
    for k in ("wp_pass", "wc_key", "wc_secret"):
        d[k + "_masked"] = mask(box.open(site[k]))
        d.pop(k, None)
    d["integ_data"] = json.loads(site["integ"] or "{}")
    return d
