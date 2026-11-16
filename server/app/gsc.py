"""Google Search Console (kliknięcia, wyświetlenia, pozycje), Google Analytics 4 i PageSpeed Insights.

Search Console: konto usługi Google (plik JSON z Google Cloud) wklejone w ustawieniach agenta. Adres e-mail
konta usługi dodajesz w Search Console każdego klienta jako użytkownika z uprawnieniem „Ograniczone”,
a w Google Analytics 4 (Administracja → Zarządzanie dostępem do usługi) z rolą „Wyświetlający”.
PageSpeed Insights: zwykły klucz API Google (opcjonalnie — bez niego agent mierzy szybkość sam).
To dane z usług Google; model AI dalej działa wyłącznie lokalnie.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

TOKEN_URL = "https://oauth2.googleapis.com/token"
GSC_URL = "https://searchconsole.googleapis.com/webmasters/v3"
PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
GA_URL = "https://analyticsdata.googleapis.com/v1beta"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly https://www.googleapis.com/auth/analytics.readonly"
_token_cache: dict[str, tuple[str, float]] = {}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def parse_service_account(raw: str) -> dict:
    """Sprawdza wklejony plik JSON konta usługi; zwraca dane albo rzuca ValueError z opisem po polsku."""
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("To nie jest poprawny plik JSON.")
    if data.get("type") != "service_account" or not data.get("client_email") or "PRIVATE KEY" not in data.get("private_key", ""):
        raise ValueError("To nie jest klucz konta usługi Google (brak client_email / private_key).")
    return data


def access_token(sa: dict, token_url: str = TOKEN_URL) -> str:
    cached = _token_cache.get(sa["client_email"])
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"iss": sa["client_email"], "scope": SCOPE, "aud": token_url,
                              "iat": now, "exp": now + 3600}).encode())
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                   "assertion": f"{header}.{claims}.{_b64(sig)}"}).encode()
    with urllib.request.urlopen(urllib.request.Request(token_url, data=body), timeout=30) as r:  # noqa: S310
        data = json.loads(r.read())
    token = data["access_token"]
    _token_cache[sa["client_email"]] = (token, time.time() + int(data.get("expires_in", 3600)))
    return token


def _query(token: str, prop: str, start: date, end: date, dimensions: list[str] | None = None,
           limit: int = 10, base: str = GSC_URL) -> list[dict]:
    url = f"{base}/sites/{urllib.parse.quote(prop, safe='')}/searchAnalytics/query"
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "rowLimit": limit}
    if dimensions:
        body["dimensions"] = dimensions
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
        return json.loads(r.read()).get("rows", [])


def search_console(sa: dict, prop: str, days: int = 7, token_url: str = TOKEN_URL, base: str = GSC_URL) -> dict:
    """Ostatnie `days` dni vs poprzednie (dane Google mają 2–3 dni opóźnienia) + najważniejsze zapytania."""
    token = access_token(sa, token_url)
    end = date.today() - timedelta(days=3)
    cur_start = end - timedelta(days=days - 1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    def totals(a, b):
        rows = _query(token, prop, a, b, base=base)
        r = rows[0] if rows else {}
        return {"clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
                "ctr": round(float(r.get("ctr", 0)) * 100, 2), "position": round(float(r.get("position", 0)), 1)}
    cur, prev = totals(cur_start, end), totals(prev_start, prev_end)
    queries = [{"query": r["keys"][0], "clicks": int(r.get("clicks", 0)), "position": round(float(r.get("position", 0)), 1)}
               for r in _query(token, prop, cur_start, end, ["query"], 8, base=base)]
    out = {"period": f"{cur_start.isoformat()} – {end.isoformat()}", "current": cur, "previous": prev,
           "queries": queries, "issues": []}
    if prev["clicks"] >= 20:
        change = (cur["clicks"] - prev["clicks"]) * 100 / prev["clicks"]
        out["change"] = round(change)
        if change <= -30:
            out["issues"].append({"level": "error", "text": f"Spadek kliknięć z Google o {abs(round(change))}% "
                                  f"({prev['clicks']} → {cur['clicks']} tygodniowo)."})
        elif change <= -15:
            out["issues"].append({"level": "warn", "text": f"Mniej kliknięć z Google o {abs(round(change))}% "
                                  f"({prev['clicks']} → {cur['clicks']})."})
    if prev["position"] and cur["position"] and cur["position"] - prev["position"] >= 3:
        out["issues"].append({"level": "warn", "text": f"Średnia pozycja w Google spadła z {prev['position']} na {cur['position']}."})
    return out


def _short(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    path = p.path if len(p.path) <= 60 else "…" + p.path[-58:]
    return f"{p.netloc}{path}" if p.netloc else url[:80]


def _entity(v) -> str:
    return str(v.get("text") or v.get("url") or "") if isinstance(v, dict) else str(v or "")


def _items(audits: dict, *names: str) -> list[dict]:
    """Elementy tabeli audytu Lighthouse (pierwsza nazwa, która istnieje — nowsze wersje zmieniają nazwy)."""
    for n in names:
        items = ((audits.get(n) or {}).get("details") or {}).get("items")
        if isinstance(items, list) and items:
            return [i for i in items if isinstance(i, dict)]
    return []


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def script_details(audits: dict) -> dict:
    """Skrypty z testu Lighthouse: najdłuższe wykonanie, najcięższe, usługi zewnętrzne, blokujące renderowanie."""
    boot = sorted(({"url": _short(str(i.get("url", ""))), "full": str(i.get("url", ""))[:300],
                    "total": round(_num(i.get("total"))), "scripting": round(_num(i.get("scripting"))),
                    "parse": round(_num(i.get("scriptParseCompile")))}
                   for i in _items(audits, "bootup-time") if str(i.get("url", "")).startswith("http") and _num(i.get("total")) >= 1),
                  key=lambda x: -x["total"])[:12]
    unused = {str(i.get("url", "")): round(_num(i.get("wastedBytes")) / 1024)
              for i in _items(audits, "unused-javascript")}
    heavy = sorted(({"url": _short(str(i.get("url", ""))), "full": str(i.get("url", ""))[:300],
                     "kb": round(_num(i.get("transferSize")) / 1024), "unused_kb": unused.get(str(i.get("url", "")), 0)}
                    for i in _items(audits, "network-requests")
                    if str(i.get("resourceType", "")).lower() == "script"), key=lambda x: -x["kb"])[:12]
    third = sorted(({"entity": _entity(i.get("entity"))[:80], "kb": round(_num(i.get("transferSize")) / 1024),
                     "blocking": round(_num(i.get("blockingTime"))), "main": round(_num(i.get("mainThreadTime")))}
                    for i in _items(audits, "third-party-summary", "third-parties-insight") if i.get("entity")),
                   key=lambda x: -(x["main"] or x["blocking"]))[:10]
    blocking = [{"url": _short(str(i.get("url", ""))), "ms": round(_num(i.get("wastedMs"))),
                 "kb": round(_num(i.get("totalBytes")) / 1024)}
                for i in _items(audits, "render-blocking-resources", "render-blocking-insight") if i.get("url")][:10]
    main = sorted(({"group": str(i.get("groupLabel") or i.get("group") or ""), "ms": round(_num(i.get("duration")))}
                   for i in _items(audits, "mainthread-work-breakdown")), key=lambda x: -x["ms"])[:7]
    return {"bootup": boot, "heavy": heavy, "third_party": third, "render_blocking": blocking, "main_thread": main}


def pagespeed(url: str, key: str, base: str = PSI_URL) -> dict:
    """Wynik Lighthouse (telefon) z PageSpeed Insights: wydajność, SEO, metryki i szczegóły skryptów."""
    q = urllib.parse.urlencode([("url", url), ("strategy", "mobile"), ("key", key),
                                ("category", "performance"), ("category", "seo"), ("category", "accessibility")])
    with urllib.request.urlopen(f"{base}?{q}", timeout=180) as r:  # noqa: S310
        data = json.loads(r.read())
    lh = data.get("lighthouseResult", {})
    cats = lh.get("categories", {})
    audits = lh.get("audits", {})

    def score(k):
        s = (cats.get(k) or {}).get("score")
        return round(s * 100) if isinstance(s, (int, float)) else None

    def disp(k):
        return (audits.get(k) or {}).get("displayValue", "")
    out = {"performance": score("performance"), "seo": score("seo"), "accessibility": score("accessibility"),
           "lcp": disp("largest-contentful-paint"), "cls": disp("cumulative-layout-shift"),
           "fcp": disp("first-contentful-paint"), "tbt": disp("total-blocking-time"), "si": disp("speed-index"),
           "tbt_ms": round(_num((audits.get("total-blocking-time") or {}).get("numericValue"))),
           "weight_kb": round(_num((audits.get("total-byte-weight") or {}).get("numericValue")) / 1024),
           "scripts": script_details(audits), "issues": []}
    if out["performance"] is not None and out["performance"] < 50:
        out["issues"].append({"level": "error", "text": f"PageSpeed (telefon): wydajność {out['performance']}/100, LCP {out['lcp']}."})
    elif out["performance"] is not None and out["performance"] < 80:
        out["issues"].append({"level": "warn", "text": f"PageSpeed (telefon): wydajność {out['performance']}/100, LCP {out['lcp']}."})
    sc = out["scripts"]
    if out["tbt_ms"] >= 600:
        out["issues"].append({"level": "error" if out["tbt_ms"] >= 1200 else "warn",
                              "text": f"Skrypty blokują stronę na telefonie przez {out['tbt']} (dobrze < 200 ms) — strona „zamarza” po wczytaniu."})
    slow = [b for b in sc["bootup"] if b["total"] >= 500]
    if slow:
        out["issues"].append({"level": "warn", "text": "Najdłużej wykonujące się skrypty: " + ", ".join(
            f"{b['url']} ({b['total'] / 1000:.1f} s)" for b in slow[:3]) + "."})
    waste = sum(h["unused_kb"] for h in sc["heavy"])
    if waste >= 150:
        out["issues"].append({"level": "warn", "text": f"Ok. {waste} KB kodu JavaScript pobieranego, ale nieużywanego na stronie głównej."})
    if sc["render_blocking"]:
        total = sum(b["ms"] for b in sc["render_blocking"])
        if total >= 300:
            out["issues"].append({"level": "warn", "text": f"{len(sc['render_blocking'])} plików blokuje wyświetlenie strony "
                                  f"(opóźnienie ok. {total / 1000:.1f} s)."})
    return out


def analytics(sa: dict, prop: str, token_url: str = TOKEN_URL, base: str = GA_URL, days: int = 7) -> dict:
    """Google Analytics 4: ostatnie `days` dni vs poprzednie, kanały, strony, urządzenia i dzienny wykres (28 dni)."""
    token = access_token(sa, token_url)
    pid = "".join(ch for ch in prop if ch.isdigit())
    url = f"{base}/properties/{pid}:runReport"

    def run(body: dict) -> list[dict]:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
            return json.loads(r.read()).get("rows", []) or []
    metrics = ["activeUsers", "sessions", "screenPageViews", "keyEvents", "engagementRate", "averageSessionDuration"]

    def totals(start: str, end: str) -> dict:
        rows = run({"dateRanges": [{"startDate": start, "endDate": end}], "metrics": [{"name": m} for m in metrics]})
        vals = [_num(v.get("value")) for v in (rows[0].get("metricValues", []) if rows else [])]
        vals += [0.0] * (len(metrics) - len(vals))
        d = dict(zip(metrics, vals))
        return {"users": int(d["activeUsers"]), "sessions": int(d["sessions"]), "views": int(d["screenPageViews"]),
                "key_events": int(d["keyEvents"]), "engagement": round(d["engagementRate"] * 100, 1),
                "duration_s": round(d["averageSessionDuration"])}
    cur = totals(f"{days}daysAgo", "yesterday")
    prev = totals(f"{2 * days}daysAgo", f"{days + 1}daysAgo")

    def top(dim: str, metric: str, limit: int) -> list[dict]:
        rows = run({"dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
                    "dimensions": [{"name": dim}], "metrics": [{"name": metric}],
                    "orderBys": [{"metric": {"metricName": metric}, "desc": True}], "limit": limit})
        return [{"name": (r.get("dimensionValues") or [{}])[0].get("value", "")[:120],
                 "value": int(_num((r.get("metricValues") or [{}])[0].get("value")))} for r in rows]
    daily_rows = run({"dateRanges": [{"startDate": "28daysAgo", "endDate": "yesterday"}],
                      "dimensions": [{"name": "date"}], "metrics": [{"name": "sessions"}],
                      "orderBys": [{"dimension": {"dimensionName": "date"}}]})
    out = {"current": cur, "previous": prev, "channels": top("sessionDefaultChannelGroup", "sessions", 6),
           "pages": top("pagePath", "screenPageViews", 8), "devices": top("deviceCategory", "sessions", 3),
           "daily": [int(_num((r.get("metricValues") or [{}])[0].get("value"))) for r in daily_rows],
           "issues": []}
    if prev["sessions"] >= 50:
        change = (cur["sessions"] - prev["sessions"]) * 100 / prev["sessions"]
        out["change"] = round(change)
        if change <= -30:
            out["issues"].append({"level": "error", "text": f"Spadek odwiedzin (Google Analytics) o {abs(round(change))}% "
                                  f"({prev['sessions']} → {cur['sessions']} sesji tygodniowo)."})
        elif change <= -15:
            out["issues"].append({"level": "warn", "text": f"Mniej odwiedzin o {abs(round(change))}% ({prev['sessions']} → {cur['sessions']})."})
    if prev["key_events"] >= 10 and cur["key_events"] <= prev["key_events"] * 0.5:
        out["issues"].append({"level": "error", "text": f"Spadek konwersji (kluczowych zdarzeń) z {prev['key_events']} do {cur['key_events']} tygodniowo — sprawdź formularze i koszyk."})
    if cur["sessions"] == 0 and prev["sessions"] > 0:
        out["issues"].append({"level": "error", "text": "Google Analytics nie zbiera danych od tygodnia — sprawdź kod śledzenia na stronie."})
    return out
