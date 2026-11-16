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


def pagespeed(url: str, key: str, base: str = PSI_URL, strategy: str = "mobile") -> dict:
    """Skrót wyniku PageSpeed (wydajność, metryki, skrypty, konkretne uwagi). Pełny wynik: speed.parse()."""
    from . import speed
    return speed.compact(speed.parse(speed.fetch(url, key, strategy, base), url))


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


# ------------------------------------------------------------------ raporty z zakresem dat (zakładki Analityka i Google)

GA_TOTALS = ["activeUsers", "newUsers", "sessions", "engagedSessions", "screenPageViews", "keyEvents", "engagementRate",
             "averageSessionDuration", "bounceRate", "transactions", "purchaseRevenue"]
GA_DAILY = ["sessions", "activeUsers", "screenPageViews", "keyEvents"]


def _ga_run(token: str, url: str, ranges: list[tuple], dims: list[str], metrics: list[str], limit: int = 0,
            order: str = "") -> dict[str, list]:
    """Zapytanie runReport; wynik: {"0": [(wymiary, metryki)], "1": [...]} dla każdego zakresu dat."""
    assert len(metrics) <= 10 and len(dims) <= 9, "limit GA4 Data API"
    body: dict = {"dateRanges": [{"startDate": a.isoformat(), "endDate": b.isoformat(), "name": str(i)}
                                 for i, (a, b) in enumerate(ranges)],
                  "metrics": [{"name": m} for m in metrics]}
    if dims:
        body["dimensions"] = [{"name": d} for d in dims]
    if limit:
        body["limit"] = limit * len(ranges)
    if order:
        body["orderBys"] = [{"metric": {"metricName": order}, "desc": True}]
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
        rows = json.loads(r.read()).get("rows", []) or []
    out: dict[str, list] = {str(i): [] for i in range(len(ranges))}
    for row in rows:
        dv = [d.get("value", "") for d in row.get("dimensionValues", [])]
        mv = [_num(m.get("value")) for m in row.get("metricValues", [])]
        rng = "0"
        if len(ranges) > 1 and len(dv) > len(dims):  # GA dopisuje wymiar „dateRange” przy kilku zakresach
            rng = dv.pop(-1 if dv[-1] in out else 0)
            rng = rng if rng in out else rng.replace("date_range_", "")
        out.setdefault(rng, []).append((dv, mv))
    return out


def ga_report(sa: dict, prop: str, cur: tuple, cmp: tuple | None, token_url: str = TOKEN_URL, base: str = GA_URL) -> dict:
    """Pełny raport GA4 dla zakresu dat i (opcjonalnie) okresu porównawczego."""
    token = access_token(sa, token_url)
    url = f"{base}/properties/{''.join(ch for ch in prop if ch.isdigit())}:runReport"
    ranges = [cur] + ([cmp] if cmp else [])
    # GA4 Data API: najwyżej 10 wskaźników w jednym zapytaniu — sumy w dwóch częściach
    part1, part2 = GA_TOTALS[:6], GA_TOTALS[6:]
    t1, t2 = _ga_run(token, url, ranges, [], part1), _ga_run(token, url, ranges, [], part2)
    tot = {k: [([], (t1.get(k) or [([], [0.0] * len(part1))])[0][1] + (t2.get(k) or [([], [0.0] * len(part2))])[0][1])]
           for k in {"0", "1"} & (set(t1) | set(t2))}

    def totals(key):
        vals = tot.get(key) or [([], [0.0] * len(GA_TOTALS))]
        d = dict(zip(GA_TOTALS, vals[0][1] + [0.0] * len(GA_TOTALS)))
        d["engagementRate"] *= 100
        d["bounceRate"] *= 100
        return d
    daily = _ga_run(token, url, ranges, ["date"], GA_DAILY)

    def series(key):
        rows = sorted(daily.get(key, []), key=lambda r: r[0][0])
        return {r[0][0]: r[1] for r in rows}

    def top(dim: str, metrics: list[str], limit: int, order: str) -> list[dict]:
        res = _ga_run(token, url, ranges, [dim], metrics, limit, order)
        prev = {tuple(dv): mv for dv, mv in res.get("1", [])}
        rows = sorted(res.get("0", []), key=lambda r: -r[1][0])[:limit]
        return [{"name": dv[0][:140] or "(brak)", "v": mv, "c": prev.get(tuple(dv))} for dv, mv in rows]
    out = {
        "cur": totals("0"), "cmp": totals("1") if cmp else None,
        "daily_cur": series("0"), "daily_cmp": series("1") if cmp else {},
        "channels": top("sessionDefaultChannelGroup", ["sessions", "keyEvents", "engagementRate"], 10, "sessions"),
        "sources": top("sessionSourceMedium", ["sessions", "keyEvents", "engagementRate"], 15, "sessions"),
        "landing": top("landingPage", ["sessions", "keyEvents", "bounceRate"], 15, "sessions"),
        "pages": top("pagePath", ["screenPageViews", "activeUsers", "averageSessionDuration"], 20, "screenPageViews"),
        "devices": top("deviceCategory", ["sessions", "keyEvents"], 5, "sessions"),
        "countries": top("country", ["activeUsers", "sessions"], 10, "activeUsers"),
        "cities": top("city", ["activeUsers", "sessions"], 10, "activeUsers"),
        "events": top("eventName", ["eventCount", "keyEvents"], 15, "eventCount"),
        "new_returning": top("newVsReturning", ["activeUsers", "sessions"], 3, "activeUsers"),
    }
    return out


def google_error(exc) -> str:
    """Komunikat Google z treści błędu HTTP (np. „Field x is not a valid metric”)."""
    try:
        body = json.loads(exc.read().decode("utf-8", errors="replace"))
        return str((body.get("error") or {}).get("message", ""))[:300]
    except Exception:  # noqa: BLE001
        return ""


def gsc_report(sa: dict, prop: str, cur: tuple, cmp: tuple | None, token_url: str = TOKEN_URL, base: str = GSC_URL) -> dict:
    """Search Console dla zakresu dat: dzienny przebieg, frazy, strony, kraje, urządzenia (z porównaniem)."""
    token = access_token(sa, token_url)

    def q(rng, dims, limit):
        return _query(token, prop, rng[0], rng[1], dims, limit, base=base)

    def tot(rng):
        r = (q(rng, None, 1) or [{}])[0]
        return {"clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
                "ctr": round(float(r.get("ctr", 0)) * 100, 2), "position": round(float(r.get("position", 0)), 1)}

    def daily(rng):
        return {r["keys"][0].replace("-", ""): [r.get("clicks", 0), r.get("impressions", 0), r.get("position", 0)]
                for r in q(rng, ["date"], 500)}

    def top(dim, limit):
        cur_rows = q(cur, [dim], limit)
        prev = {r["keys"][0]: r for r in q(cmp, [dim], limit * 4)} if cmp else {}
        return [{"name": r["keys"][0][:160], "clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
                 "ctr": round(float(r.get("ctr", 0)) * 100, 1), "position": round(float(r.get("position", 0)), 1),
                 "c": prev.get(r["keys"][0])} for r in cur_rows]
    return {"cur": tot(cur), "cmp": tot(cmp) if cmp else None, "daily_cur": daily(cur),
            "daily_cmp": daily(cmp) if cmp else {}, "queries": top("query", 30), "pages": top("page", 20),
            "countries": top("country", 10), "devices": top("device", 3)}
