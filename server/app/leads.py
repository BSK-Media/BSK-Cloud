"""Automatyczne wyszukiwanie firm (potencjalnych klientów) z publicznej bazy OpenStreetMap.

Ustawiasz w panelu branżę i miasto (np. „dentysta”, „Wrocław”). Raz na dobę agent pyta publiczne API
Overpass o firmy z tej branży w tym mieście, które mają stronę WWW, i dodaje kilka nowych do listy
„potencjalny klient”. Dalej działa zwykły tryb agenta: audyt strony, adres e-mail, gotowy e-mail
do Twojej akceptacji. Dane o firmach pochodzą z OpenStreetMap (licencja ODbL).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .db import Database, now_iso

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
UA = "BSK-Cloud-Agent/1.0 (+https://cloud.bskmedia.pl)"

# branża (po polsku) -> znaczniki OpenStreetMap
CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "dentysta": [("amenity", "dentist"), ("healthcare", "dentist")],
    "lekarz / przychodnia": [("amenity", "doctors"), ("amenity", "clinic")],
    "fizjoterapia": [("healthcare", "physiotherapist")],
    "weterynarz": [("amenity", "veterinary")],
    "apteka": [("amenity", "pharmacy")],
    "restauracja": [("amenity", "restaurant")],
    "kawiarnia": [("amenity", "cafe")],
    "hotel / pensjonat": [("tourism", "hotel"), ("tourism", "guest_house")],
    "fryzjer": [("shop", "hairdresser")],
    "salon kosmetyczny": [("shop", "beauty")],
    "siłownia / fitness": [("leisure", "fitness_centre")],
    "warsztat samochodowy": [("shop", "car_repair")],
    "salon samochodowy": [("shop", "car")],
    "kancelaria prawna": [("office", "lawyer")],
    "biuro rachunkowe": [("office", "accountant")],
    "nieruchomości": [("office", "estate_agent")],
    "firma budowlana": [("craft", "builder"), ("office", "construction_company")],
    "sklep meblowy": [("shop", "furniture")],
    "kwiaciarnia": [("shop", "florist")],
    "przedszkole": [("amenity", "kindergarten")],
    "szkoła językowa": [("amenity", "language_school")],
    "agencja ubezpieczeniowa": [("office", "insurance")],
    "piekarnia / cukiernia": [("shop", "bakery"), ("shop", "pastry")],
    "optyk": [("shop", "optician")],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS lead_searches (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    city TEXT NOT NULL,
    per_day INTEGER NOT NULL DEFAULT 5,
    active INTEGER NOT NULL DEFAULT 1,
    last_run TEXT,
    found INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def init(db: Database) -> None:
    db.conn().executescript(SCHEMA)


def clean_city(city: str) -> str:
    """Nazwa miasta do zapytania Overpass: tylko litery, cyfry, spacje, kropki, apostrofy i myślniki."""
    return re.sub(r"[^\w\s.'-]", "", city, flags=re.UNICODE).strip()[:80]


ALL = "*"
SEP = "|"


def parse_categories(value: str) -> list[str]:
    """Zapisane branże wyszukiwania: „*” = wszystkie, inaczej lista rozdzielona „|” (stare wpisy: jedna branża)."""
    if value == ALL:
        return list(CATEGORIES)
    return [c for c in value.split(SEP) if c in CATEGORIES]


def categories_label(value: str) -> str:
    if value == ALL:
        return "wszystkie branże"
    cats = parse_categories(value)
    return ", ".join(cats) if len(cats) <= 4 else f"{', '.join(cats[:3])} i {len(cats) - 3} innych"


def category_of(tags: dict, cats: list[str]) -> str:
    """Branża znalezionej firmy (po znacznikach OpenStreetMap)."""
    for c in cats:
        if any(tags.get(k) == v for k, v in CATEGORIES[c]):
            return c
    return ""


def build_query(category: str, city: str, limit: int = 200) -> str:
    cats = parse_categories(category)
    tags = [t for c in cats for t in CATEGORIES[c]]
    city = clean_city(city)
    limit = limit if len(cats) <= 3 else max(limit, 600)
    parts = []
    for k, v in tags:
        for site_key in ("website", "contact:website"):
            parts.append(f'nwr["{k}"="{v}"]["{site_key}"](area.a);')
    return (f'[out:json][timeout:{90 if len(cats) <= 3 else 170}];area["name"="{city}"]["boundary"="administrative"]->.a;'
            f'({"".join(parts)});out tags {int(limit)};')


def search(category: str, city: str, url: str = OVERPASS_URL, timeout: float = 180) -> list[dict]:
    """Firmy ze stroną WWW: [{name, website, email, phone, address}] (bez duplikatów domen)."""
    data = urllib.parse.urlencode({"data": build_query(category, city)}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — publiczne API Overpass
        elements = json.loads(r.read()).get("elements", [])
    out, hosts = [], set()
    for el in elements:
        t = el.get("tags", {})
        site = (t.get("website") or t.get("contact:website") or "").strip()
        if not site:
            continue
        if "://" not in site:
            site = "https://" + site
        host = (urllib.parse.urlsplit(site).hostname or "").removeprefix("www.")
        if not host or host in hosts or host.endswith(("facebook.com", "instagram.com", "google.com", "booksy.com",
                                                       "znanylekarz.pl", "olx.pl", "linktr.ee")):
            continue
        hosts.add(host)
        email = (t.get("email") or t.get("contact:email") or "").split(";")[0].strip()
        addr = " ".join(x for x in (t.get("addr:street", ""), t.get("addr:housenumber", ""), t.get("addr:city", "")) if x)
        out.append({"name": t.get("name", host)[:120], "website": site, "host": host,
                    "category": category_of(t, parse_categories(category)),
                    "email": email if re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email) else "",
                    "phone": (t.get("phone") or t.get("contact:phone") or "")[:40], "address": addr[:200]})
    return out


def run_search(db: Database, row, add_site, url: str = OVERPASS_URL, progress=None) -> int:
    """Jedno wyszukiwanie: dodaje do `per_day` nowych firm (bez stron już znanych agentowi)."""
    if progress:
        progress(f"Szukam nowych firm: {categories_label(row['category'])}, {row['city']}")
    try:
        found = search(row["category"], row["city"], url)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        db.run("UPDATE lead_searches SET last_run = ?, last_error = ? WHERE id = ?", now_iso(), str(exc)[:300], row["id"])
        return 0
    known = set()
    for s in db.all("SELECT url FROM sites"):
        known.add((urllib.parse.urlsplit(s["url"]).hostname or "").removeprefix("www."))
    added = 0
    for firm in found:
        if added >= row["per_day"]:
            break
        if firm["host"] in known:
            continue
        add_site(firm, f"{firm.get('category') or categories_label(row['category'])}, {row['city']}")
        known.add(firm["host"])
        added += 1
    db.run("UPDATE lead_searches SET last_run = ?, found = found + ?, last_error = '' WHERE id = ?",
           now_iso(), added, row["id"])
    return added


def due_search(db: Database):
    """Wyszukiwanie, które nie było uruchamiane od doby (najstarsze pierwsze)."""
    old = (datetime.now(timezone.utc) - timedelta(hours=23)).isoformat()
    return db.one("SELECT * FROM lead_searches WHERE active = 1 AND (last_run IS NULL OR last_run < ?)"
                  " ORDER BY last_run IS NOT NULL, last_run LIMIT 1", old)
