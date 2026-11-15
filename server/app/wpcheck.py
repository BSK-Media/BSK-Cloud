"""WordPress i WooCommerce stron klientów.

Bez kluczy: czy to WordPress, wersja (jeśli strona ją pokazuje), czy jest WooCommerce.
Z hasłem aplikacji WordPress (Użytkownicy → Profil → Hasła aplikacji): lista wtyczek i ich wersje,
porównane z najnowszymi z WordPress.org.
Z kluczem API WooCommerce (WooCommerce → Ustawienia → Zaawansowane → REST API, uprawnienie „Odczyt”):
wersje PHP/WordPress/wtyczek ze statusu systemu i zamówienia z ostatniej doby (nieudane, spadek sprzedaży).
Wszystko tylko do odczytu.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (compatible; BSK-Monitor/1.0; +https://cloud.bskmedia.pl)"
WP_API = "https://api.wordpress.org"


def _get(url: str, auth: tuple[str, str] | None = None, timeout: float = 30):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — adresy stron klientów
        raw = r.read(8 * 1024 * 1024)
        return r.status, raw, {k.lower(): v for k, v in r.headers.items()}


def _json(url: str, auth=None, timeout: float = 30):
    _, raw, _ = _get(url, auth, timeout)
    return json.loads(raw.decode("utf-8", errors="replace"))


def _root(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _vt(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def latest_core(api: str = WP_API) -> str:
    try:
        offers = _json(f"{api}/core/version-check/1.7/").get("offers", [])
        return offers[0].get("current", "") if offers else ""
    except (OSError, urllib.error.URLError, ValueError):
        return ""


def latest_plugin(slug: str, api: str = WP_API) -> str:
    try:
        data = _json(f"{api}/plugins/info/1.2/?action=plugin_information&request%5Bslug%5D={urllib.parse.quote(slug)}")
        return str(data.get("version", "")) if isinstance(data, dict) else ""
    except (OSError, urllib.error.URLError, ValueError):
        return ""  # wtyczka płatna albo spoza WordPress.org


def check(url: str, wp_user: str = "", wp_pass: str = "", wc_key: str = "", wc_secret: str = "",
          api: str = WP_API) -> dict:
    """Wynik: {is_wp, version, latest, woo, plugins:[...], outdated:[...], php, orders:{...}, issues:[...]}"""
    root = _root(url)
    out: dict = {"is_wp": False, "version": "", "latest": "", "woo": False, "plugins": [], "outdated": [],
                 "php": "", "orders": None, "issues": [], "checked_at": datetime.now(timezone.utc).isoformat()}
    issues = out["issues"]
    try:
        info = _json(root + "/wp-json/", timeout=20)
        if isinstance(info, dict) and "namespaces" in info:
            out["is_wp"] = True
            out["woo"] = any(str(n).startswith("wc/") for n in info.get("namespaces", []))
    except (OSError, urllib.error.URLError, ValueError):
        pass
    if not out["is_wp"]:
        try:
            _, raw, _ = _get(url, timeout=20)
            html = raw.decode("utf-8", errors="replace")
            if "/wp-content/" in html or "/wp-includes/" in html:
                out["is_wp"] = True
                out["woo"] = "woocommerce" in html.lower()
        except (OSError, urllib.error.URLError, ValueError):
            pass
    if not out["is_wp"]:
        return out
    try:
        _, raw, _ = _get(root + "/feed/", timeout=20)
        m = re.search(r"wordpress\.org/\?v=([\d.]+)", raw.decode("utf-8", errors="replace"))
        if m:
            out["version"] = m.group(1)
    except (OSError, urllib.error.URLError, ValueError):
        pass
    out["latest"] = latest_core(api)

    if wp_user and wp_pass:
        try:
            plugins = _json(root + "/wp-json/wp/v2/plugins?per_page=100&context=edit", (wp_user, wp_pass))
            for p in plugins[:60] if isinstance(plugins, list) else []:
                slug = str(p.get("plugin", "")).split("/")[0]
                item = {"name": re.sub(r"<[^>]+>", "", str(p.get("name", slug)))[:80], "slug": slug,
                        "version": str(p.get("version", "")), "active": p.get("status") == "active", "latest": ""}
                item["latest"] = latest_plugin(slug, api) if slug else ""
                out["plugins"].append(item)
        except urllib.error.HTTPError as exc:
            issues.append({"level": "warn", "text": f"WordPress odrzucił hasło aplikacji (HTTP {exc.code}) — sprawdź dane w ustawieniach strony."})
        except (OSError, urllib.error.URLError, ValueError):
            issues.append({"level": "warn", "text": "Nie udało się pobrać listy wtyczek WordPressa."})

    if out["woo"] and wc_key and wc_secret:
        auth = (wc_key, wc_secret)
        try:
            status = _json(root + "/wp-json/wc/v3/system_status", auth)
            env = status.get("environment", {})
            out["php"] = str(env.get("php_version", ""))
            out["version"] = out["version"] or str(env.get("wp_version", ""))
            if not out["plugins"]:
                for p in status.get("active_plugins", [])[:60]:
                    out["plugins"].append({"name": str(p.get("name", ""))[:80], "slug": str(p.get("plugin", "")).split("/")[0],
                                           "version": str(p.get("version", "")), "active": True,
                                           "latest": str(p.get("version_latest", ""))})
            out["orders"] = _orders(root, auth)
        except urllib.error.HTTPError as exc:
            issues.append({"level": "warn", "text": f"WooCommerce odrzucił klucz API (HTTP {exc.code}) — sprawdź klucz w ustawieniach strony."})
        except (OSError, urllib.error.URLError, ValueError):
            issues.append({"level": "warn", "text": "Nie udało się połączyć z API WooCommerce."})

    # ocena
    if out["version"] and out["latest"] and _vt(out["version"]) < _vt(out["latest"]):
        major = _vt(out["version"])[:2] < _vt(out["latest"])[:2]
        issues.append({"level": "error" if major else "warn",
                       "text": f"WordPress {out['version']} — dostępna wersja {out['latest']}."})
    for p in out["plugins"]:
        if p["latest"] and p["version"] and _vt(p["version"]) < _vt(p["latest"]):
            out["outdated"].append(p)
    if out["outdated"]:
        names = ", ".join(f"{p['name']} {p['version']}→{p['latest']}" for p in out["outdated"][:6])
        issues.append({"level": "error" if len(out["outdated"]) >= 5 else "warn",
                       "text": f"Nieaktualne wtyczki ({len(out['outdated'])}): {names}."})
    inactive = [p for p in out["plugins"] if not p["active"]]
    if len(inactive) >= 3:
        issues.append({"level": "warn", "text": f"{len(inactive)} nieaktywnych wtyczek — warto je usunąć (bezpieczeństwo)."})
    if out["php"] and _vt(out["php"])[:2] < (8, 1):
        issues.append({"level": "error", "text": f"PHP {out['php']} — wersja bez wsparcia bezpieczeństwa, zalecana 8.2 lub nowsza."})
    o = out["orders"]
    if o:
        if o["failed"]:
            issues.append({"level": "error", "text": f"Nieudane zamówienia w ciągu doby: {o['failed']} — sprawdź płatności."})
        if o["avg_week"] >= 3 and o["day"] == 0:
            issues.append({"level": "error", "text": f"Brak zamówień od doby (średnio {o['avg_week']:.0f} dziennie) — sprawdź sklep."})
        elif o["avg_week"] >= 5 and o["day"] < o["avg_week"] * 0.4:
            issues.append({"level": "warn", "text": f"Mniej zamówień niż zwykle: {o['day']} (średnio {o['avg_week']:.0f} dziennie)."})
    return out


def _orders(root: str, auth: tuple[str, str]) -> dict:
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    rows, page = [], 1
    while page <= 10:
        batch = _json(f"{root}/wp-json/wc/v3/orders?after={week_ago}&per_page=100&page={page}"
                      "&_fields=id,status,date_created_gmt,total", auth)
        if not isinstance(batch, list) or not batch:
            break
        rows += batch
        if len(batch) < 100:
            break
        page += 1
    day_ago = now - timedelta(days=1)

    def when(o):
        try:
            return datetime.fromisoformat(str(o.get("date_created_gmt", ""))).replace(tzinfo=timezone.utc)
        except ValueError:
            return now - timedelta(days=30)
    recent = [o for o in rows if when(o) > day_ago]
    ok = [o for o in rows if o.get("status") not in ("failed", "cancelled", "pending")]
    return {"day": len([o for o in recent if o.get("status") not in ("failed", "cancelled", "pending")]),
            "failed": len([o for o in recent if o.get("status") == "failed"]),
            "pending": len([o for o in recent if o.get("status") == "pending"]),
            "avg_week": len(ok) / 7, "week": len(ok)}
