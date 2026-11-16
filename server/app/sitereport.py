"""Raport strony w panelu agenta: zakładki, zakresy dat z porównaniem, dane do wykresów, zbiorcza lista błędów."""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta



TABS = [("przeglad", "Przegląd", "bot"), ("bledy", "Błędy i zalecenia", "list"), ("szybkosc", "Szybkość", "clock"), ("siec", "Sieć (Network)", "network"),
        ("analityka", "Analityka", "chart"), ("google", "Wyniki w Google", "search"), ("seo", "SEO i udostępnianie", "link"),
        ("technologia", "Technologia", "cpu"), ("integracje", "Integracje", "key")]
CLIENT_ONLY = {"analityka", "google", "integracje"}
PRESETS = [("7", "Ostatnie 7 dni"), ("28", "Ostatnie 28 dni"), ("90", "Ostatnie 90 dni"), ("180", "Ostatnie 6 miesięcy"),
           ("365", "Ostatnie 12 miesięcy"), ("ten-miesiac", "Ten miesiąc"), ("poprzedni-miesiac", "Poprzedni miesiąc"),
           ("wlasny", "Własny zakres")]
COMPARE = [("poprzedni", "z poprzednim okresem"), ("rok", "z tym samym okresem rok wcześniej"), ("brak", "bez porównania")]
MONTHS = ["", "stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
          "października", "listopada", "grudnia"]


def tabs_for(kind: str) -> list[tuple]:
    return [t for t in TABS if kind == "client" or t[0] not in CLIENT_ONLY]


def _d(v: str) -> date | None:
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        return None


def pl_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month]} {d.year}"


def date_range(preset: str, od: str, do: str, compare: str, today: date | None = None) -> dict:
    """Zakres bieżący i porównawczy z parametrów formularza (domyślnie 28 dni do wczoraj, porównanie z poprzednim)."""
    today = today or date.today()
    end = today - timedelta(days=1)
    if preset == "wlasny" and _d(od) and _d(do):
        start, end = sorted((_d(od), _d(do)))
        end = min(end, today)
    elif preset == "ten-miesiac":
        start = end.replace(day=1)
    elif preset == "poprzedni-miesiac":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    else:
        preset = preset if preset in {p for p, _ in PRESETS} else "28"
        days = int(preset) if preset.isdigit() else 28
        start = end - timedelta(days=days - 1)
    start = max(start, end - timedelta(days=730))
    length = (end - start).days + 1
    cmp = None
    if compare == "rok":
        try:
            cmp = (start.replace(year=start.year - 1), end.replace(year=end.year - 1))
        except ValueError:  # 29 lutego
            cmp = (start - timedelta(days=365), end - timedelta(days=365))
    elif compare != "brak":
        compare = "poprzedni"
        cmp = (start - timedelta(days=length), start - timedelta(days=1))
    return {"preset": preset, "compare": compare, "cur": (start, end), "cmp": cmp, "days": length,
            "label": f"{pl_date(start)} – {pl_date(end)}",
            "cmp_label": f"{pl_date(cmp[0])} – {pl_date(cmp[1])}" if cmp else ""}


def day_list(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def aligned(rng: dict, daily_cur: dict, daily_cmp: dict, idx: int) -> tuple[list[date], list, list]:
    """Dni bieżącego okresu i wartości (bieżące, porównawcze dzień-do-dnia) z map {RRRRMMDD: [metryki]}."""
    days = day_list(*rng["cur"])
    cur = [((daily_cur.get(d.strftime("%Y%m%d")) or [0] * (idx + 1))[idx]) for d in days]
    cmp = []
    if rng["cmp"]:
        cdays = day_list(*rng["cmp"])
        cmp = [((daily_cmp.get(d.strftime("%Y%m%d")) or [0] * (idx + 1))[idx]) for d in cdays][:len(days)]
    return days, cur, cmp


def change(cur, prev) -> float | None:
    if prev in (None, 0) or cur is None:
        return None
    return round((cur - prev) * 100 / prev, 1)


class Cache:
    """Wyniki zapytań do Google na 15 minut (przełączanie zakładek nie odpytuje API za każdym razem)."""

    def __init__(self, ttl: int = 900):
        self.ttl, self.data = ttl, {}

    def get(self, key, fn):
        hit = self.data.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        val = fn()
        self.data[key] = (time.time(), val)
        if len(self.data) > 200:
            for k in sorted(self.data, key=lambda k: self.data[k][0])[:50]:
                self.data.pop(k, None)
        return val


# ------------------------------------------------------------------ błędy i zalecenia (jedna lista ze wszystkich źródeł)

FIX = [
    (r"HTTPS", "Włącz certyfikat SSL (np. Let's Encrypt w panelu hostingu) i przekierowanie 301 z http:// na https://."),
    (r"opisu strony|meta description|bez opisu meta", "Dopisz w <head> <meta name=\"description\"> — 70–160 znaków zachęcających do kliknięcia, z główną frazą."),
    (r"Tytuł ma|Brak tytułu", "Tytuł 30–60 znaków: najważniejsza fraza + nazwa firmy / miasto, np. „Dentysta Wrocław — Gabinet Uśmiech”."),
    (r"nagłówków H1|nagłówka H1", "Na każdej podstronie dokładnie jeden H1 opisujący jej temat; pozostałe nagłówki zamień na H2/H3."),
    (r"Open Graph|og:image|Obrazek podglądu", "Dodaj znaczniki og:title, og:description i og:image (1200×630 px, < 1 MB) — gotowy kod w zakładce SEO i udostępnianie."),
    (r"viewport", "Dodaj <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> i sprawdź wygląd na telefonie."),
    (r"canonical", "Dodaj <link rel=\"canonical\"> z docelowym adresem strony (chroni przed duplikatami)."),
    (r"robots\.txt", "Utwórz plik /robots.txt z adresem mapy strony (Sitemap: https://…/sitemap.xml)."),
    (r"sitemap", "Wygeneruj mapę strony (WordPress: Yoast / Rank Math) i zgłoś ją w Google Search Console."),
    (r"alt", "Uzupełnij opisy alt obrazów — krótko, co przedstawia zdjęcie (SEO grafiki i dostępność)."),
    (r"kompres", "Włącz kompresję gzip/brotli na serwerze (hosting / .htaccess / wtyczka cache)."),
    (r"Serwer odpowiada", "Włącz cache strony (np. LiteSpeed Cache, WP Rocket), sprawdź hosting i liczbę wtyczek; rozważ CDN."),
    (r"ciężk|Ciężkie obrazy|WebP|srcset|leniwego", "Kompresuj zdjęcia i zapisuj w WebP/AVIF, dodaj loading=\"lazy\" i srcset (wtyczka np. ShortPixel, Imagify)."),
    (r"jQuery|Bootstrap|HTML 4|<font>|tabelach|HTML5|Flash", "Kod strony jest przestarzały — poprawki doraźne niewiele dadzą; rekomendacja: nowa strona na aktualnym szablonie."),
    (r"stopce rok", "Zaktualizuj rok w stopce (najlepiej automatycznie) i odśwież treści — klienci oceniają, czy firma działa."),
    (r"Universal Analytics|Google Analytics|Tag Manager", "Załóż Google Analytics 4 (najlepiej przez Tag Managera) i ustaw kluczowe zdarzenia: formularz, telefon, zakup."),
    (r"danych strukturalnych", "Dodaj dane strukturalne JSON-LD (LocalBusiness / Organization, godziny, adres, opinie) — lepsza widoczność w Google i Mapach."),
    (r"tel:", "Zamień numer telefonu na link <a href=\"tel:+48…\"> — na telefonie wystarczy jedno dotknięcie."),
    (r"formularza", "Dodaj krótki formularz kontaktowy lub widoczny adres e-mail na stronie głównej."),
    (r"wezwania do działania", "Dodaj wyraźny przycisk w pierwszym ekranie, np. „Umów wizytę”, „Zamów wycenę”, „Zadzwoń”."),
    (r"mediów społecznościowych", "Dodaj ikony z linkami do profili firmy (Facebook, Instagram, LinkedIn) w nagłówku lub stopce."),
    (r"RODO|prywatności", "Dodaj politykę prywatności i baner zgody na cookies (Consent Mode v2 przy Google Ads / Analytics)."),
    (r"favicon|apple-touch-icon", "Dodaj ikonę strony (favicon 32×32 i apple-touch-icon 180×180) z logo firmy."),
    (r"Mało tekstu|mało tekstu|śródtytułów", "Rozbuduj treść: opis usług, korzyści, lokalizacja, FAQ — z podziałem na sekcje H2."),
    (r"PHP", "Zaktualizuj PHP na hostingu do 8.2+ (po kopii zapasowej i teście wtyczek)."),
    (r"nagłówków bezpieczeństwa", "Dodaj nagłówki HSTS, X-Content-Type-Options i Content-Security-Policy (hosting / Cloudflare / .htaccess)."),
    (r"Mieszana zawartość", "Zmień adresy http:// zasobów na https:// (np. wtyczka Better Search Replace)."),
    (r"blokują stronę|wykonujące się skrypty|JavaScriptu", "Opóźnij lub usuń zbędne skrypty (Tag Manager, czaty, piksele, slidery): ładowanie po interakcji, defer, usunięcie nieużywanych wtyczek."),
    (r"blokuje wyświetlenie|blokujących", "Wczytuj CSS krytyczny w <head>, resztę asynchronicznie; skrypty z atrybutem defer."),
    (r"Główna treść pojawia", "Przyspiesz główny element: mniejszy i nowocześniejszy obraz, fetchpriority=\"high\", bez lazy-load dla pierwszego ekranu, szybszy serwer."),
    (r"Wordpress|WordPress \d|wtyczki|Nieaktualne", "Zaktualizuj WordPress i wtyczki (po kopii zapasowej), usuń nieaktywne wtyczki."),
    (r"kliknięć|pozycja w Google|odwiedzin", "Sprawdź w Search Console, które frazy i strony straciły ruch; zaktualizuj treści i linki wewnętrzne."),
    (r"konwersji|zamówień", "Sprawdź formularze, koszyk i płatności (test zakupu), kampanie i kody śledzenia."),
    (r"kreatorze", "Strona w kreatorze ma ograniczenia SEO i szybkości — rekomendacja: przeniesienie na WordPress / własny kod."),
    (r"tytułem|bez opisu meta:|bez nagłówka H1|podstron", "Każda podstrona potrzebuje unikalnego tytułu, opisu meta i jednego H1 dopasowanego do jej treści."),
]


def fix_for(text: str) -> str:
    for pat, hint in FIX:
        if re.search(pat, text, re.I):
            return hint
    return ""


def all_issues(audit_data: dict | None, integ: dict, psi_full: dict | None) -> list[dict]:
    """Jedna lista: audyt + integracje + rekomendacje PageSpeed (Lighthouse) z opisem „jak naprawić”."""
    out, seen = [], set()

    def add(level, area, text, fix="", source="", rows=None, gain=""):
        key = (area, text[:120])
        if key in seen:
            return
        seen.add(key)
        out.append({"level": level, "area": area, "text": text, "fix": fix or fix_for(text), "source": source,
                    "rows": rows or [], "gain": gain})
    for i in (audit_data or {}).get("issues", []):
        add(i["level"], i.get("area", "Audyt"), i["text"], source="audyt")
    for i in integ.get("issues", []):
        add(i["level"], i.get("area", "Integracje"), i["text"], source="integracje")
    if psi_full:
        for key, area in (("opportunities", "Szybkość (Google)"), ("seo_findings", "SEO (Google)"),
                          ("a11y_findings", "Dostępność"), ("bp_findings", "Dobre praktyki")):
            for f in psi_full.get(key, []):
                gain = " · ".join(x for x in ((f"do zyskania {f['save_ms'] / 1000:.1f} s".replace(".", ",") if f["save_ms"] >= 100 else ""),
                                               (f"{f['save_kb']} KB mniej" if f["save_kb"] >= 10 else "")) if x)
                add(f["level"], area, f["title"] + (f" — {f['display']}" if f["display"] else ""), f["desc"], "PageSpeed",
                    f["rows"], gain)
    order = {"error": 0, "warn": 1}
    out.sort(key=lambda i: order.get(i["level"], 2))
    return out


def latest_psi(db, site_id: int, strategy: str) -> dict | None:
    row = db.one("SELECT * FROM psi_runs WHERE site_id = ? AND strategy = ? ORDER BY ts DESC LIMIT 1", site_id, strategy)
    if not row:
        return None
    return {"ts": row["ts"], **json.loads(row["data"])}


def psi_history(db, site_id: int, strategy: str) -> list:
    return db.all("SELECT ts, performance, lcp_ms, tbt_ms, cls FROM psi_runs WHERE site_id = ? AND strategy = ?"
                  " ORDER BY ts DESC LIMIT 60", site_id, strategy)[::-1]


CHANNELS_PL = {"Organic Search": "Wyszukiwanie bezpłatne", "Direct": "Bezpośrednio", "Paid Search": "Wyszukiwanie płatne (Ads)",
               "Organic Social": "Social media", "Paid Social": "Social media płatne", "Referral": "Odesłania z innych stron",
               "Email": "E-mail", "Display": "Reklamy displayowe", "Unassigned": "Nieprzypisane", "Organic Video": "Wideo",
               "Paid Video": "Wideo płatne", "Organic Shopping": "Zakupy Google", "Paid Shopping": "Zakupy płatne",
               "Cross-network": "Kampanie Performance Max", "Affiliates": "Afiliacja", "SMS": "SMS",
               "Mobile Push Notifications": "Powiadomienia push", "Paid Other": "Inne płatne"}
DEVICES_PL = {"mobile": "telefon", "desktop": "komputer", "tablet": "tablet", "MOBILE": "telefon", "DESKTOP": "komputer",
              "TABLET": "tablet", "new": "nowi", "returning": "powracający", "(not set)": "nieokreślone"}


def pl_name(name: str) -> str:
    return CHANNELS_PL.get(name) or DEVICES_PL.get(name) or name


NET_TYPES = ["Dokument", "JS", "CSS", "Obraz", "Czcionka", "Fetch/XHR", "Media", "Inne"]
NET_SORT = [("start", "kolejność"), ("czas", "najdłużej"), ("rozmiar", "największe")]


def filter_network(rows: list[dict], typ: str = "", q: str = "", sort: str = "start", third: bool = False) -> list[dict]:
    out = [r for r in rows if (not typ or r["type"] == typ or (typ == "Inne" and r["type"] not in NET_TYPES[:-1]))
           and (not q or q.lower() in r["url"].lower()) and (not third or r["third"])]
    if sort == "czas":
        out.sort(key=lambda r: -r["ms"])
    elif sort == "rozmiar":
        out.sort(key=lambda r: -r["kb"])
    return out
