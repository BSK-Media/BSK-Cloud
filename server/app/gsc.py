"""Google Search Console (kliknięcia, wyświetlenia, pozycje) i PageSpeed Insights.

Search Console: konto usługi Google (plik JSON z Google Cloud) wklejone w ustawieniach agenta. Adres e-mail
konta usługi dodajesz w Search Console każdego klienta jako użytkownika z uprawnieniem „Ograniczone”.
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
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
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


def pagespeed(url: str, key: str, base: str = PSI_URL) -> dict:
    """Wynik Lighthouse (telefon) z PageSpeed Insights: wydajność, SEO, LCP, CLS."""
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
    out = {"performance": score("performance"), "seo": score("seo"), "accessibility": score("accessibility"),
           "lcp": (audits.get("largest-contentful-paint") or {}).get("displayValue", ""),
           "cls": (audits.get("cumulative-layout-shift") or {}).get("displayValue", ""), "issues": []}
    if out["performance"] is not None and out["performance"] < 50:
        out["issues"].append({"level": "error", "text": f"PageSpeed (telefon): wydajność {out['performance']}/100, LCP {out['lcp']}."})
    elif out["performance"] is not None and out["performance"] < 80:
        out["issues"].append({"level": "warn", "text": f"PageSpeed (telefon): wydajność {out['performance']}/100, LCP {out['lcp']}."})
    return out
