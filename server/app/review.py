"""Pełna ocena strony dla BSK Media — ponad zwykły audyt SEO.

- nowoczesność i technologia: CMS / kreator, wiek bibliotek (jQuery, Bootstrap), przestarzały HTML, Flash,
  rok w stopce, nowoczesne formaty obrazów, analityka, dane strukturalne, nagłówki bezpieczeństwa,
- konwersja: telefon, formularz, wezwania do działania, media społecznościowe, polityka prywatności,
- treść i podstrony: ilość tekstu, śródtytuły, do kilku podstron (tytuły, opisy, H1, cienkie strony),
- wygląd: zrzuty ekranu (komputer i telefon) z kontenera „zrzuty” (Gotenberg / Chromium) oceniane przez
  lokalny model AI rozpoznający obrazy,
- rekomendacja: oceny 0–10 w 8 obszarach, werdykt (przebudowa / odświeżenie / optymalizacja …), usługi
  z priorytetem, argumenty do rozmowy i szybkie poprawki.
Bez modelu AI agent wylicza ocenę i werdykt sam (reguły poniżej); model je poprawia i opisuje.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from . import ai

# ustawiane przy starcie portalu (main.py)
CONFIG: dict = {"screenshot_url": "", "dir": None, "pagespeed": None}

AREAS = [("seo", "SEO"), ("wydajnosc", "Szybkość"), ("wyglad", "Wygląd i nowoczesność"), ("mobilnosc", "Telefony"),
         ("tresc", "Treść"), ("konwersja", "Kontakt i sprzedaż"), ("technologia", "Technologia i bezpieczeństwo"),
         ("marka", "Marka i wizerunek")]
AREA_OF = {"SEO": "seo", "Podstrony": "seo", "Wydajność": "wydajnosc", "Mobilność": "mobilnosc",
           "Bezpieczeństwo": "technologia", "Dostępność": "technologia", "Technologia": "technologia",
           "Nowoczesność": "wyglad", "Treść": "tresc", "Konwersja": "konwersja", "Analityka": "konwersja",
           "Marka": "marka"}
VERDICTS = {
    "przebudowa": "Przebudowa strony",
    "odswiezenie": "Odświeżenie wyglądu",
    "optymalizacja": "Optymalizacja (SEO i szybkość)",
    "poprawki": "Drobne poprawki",
    "dobra": "Strona w dobrym stanie",
}
SERVICES = ["Nowa strona internetowa", "Odświeżenie wyglądu strony", "Rebranding (logo i identyfikacja)",
            "Pozycjonowanie SEO", "SEO lokalne i wizytówka Google", "Optymalizacja szybkości", "Wersja na telefony",
            "Sklep internetowy", "Teksty i treści na stronę", "Kampanie Google Ads", "Analityka i pomiar konwersji",
            "Opieka techniczna i aktualizacje", "Bezpieczeństwo i kopie zapasowe", "Social media"]

BUILDERS = [("Wix", ("wixstatic.com", "x-wix-")), ("Jimdo", ("jimdo",)), ("Webnode", ("webnode",)),
            ("Weebly", ("weebly",)), ("Squarespace", ("squarespace",)), ("Shopify", ("cdn.shopify.com",)),
            ("Shoper", ("shoper",)), ("IdoSell", ("idosell", "iai-shop")), ("PrestaShop", ("prestashop",)),
            ("Joomla", ("/media/jui/", "joomla")), ("Drupal", ("drupal",)), ("WordPress", ("/wp-content/", "/wp-includes/"))]
PAGE_BUILDERS = [("Elementor", "elementor"), ("Divi", "et_pb_"), ("WPBakery", "vc_row"), ("Beaver Builder", "fl-builder")]
SOCIAL = {"facebook.com": "Facebook", "instagram.com": "Instagram", "linkedin.com": "LinkedIn",
          "youtube.com": "YouTube", "tiktok.com": "TikTok", "x.com/": "X", "twitter.com": "X"}
CTA_WORDS = ("umów", "zarezerwuj", "rezerwacja", "zamów", "kup", "zadzwoń", "napisz", "skontaktuj", "wycena",
             "zapytaj", "zapisz", "sprawdź ofertę", "kontakt", "book", "order", "contact")
PRIORITY_PAGES = ("kontakt", "contact", "oferta", "uslugi", "usługi", "o-nas", "about", "cennik", "menu", "blog")


class _Tech(HTMLParser):
    """Zbiera sygnały o nowoczesności, konwersji i treści strony."""
    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: dict[str, int] = {}
        self.generator = ""
        self.icon = False
        self.touch_icon = False
        self.viewport = ""
        self.ld_types: list[str] = []
        self.scripts: list[str] = []
        self.inline_js = ""
        self.links: list[tuple[str, str]] = []
        self.styles_attr = 0
        self.lazy = 0
        self.srcset = 0
        self.modern_img = 0
        self.img_src: list[str] = []
        self.forms = 0
        self.inputs_contact = 0
        self.words = 0
        self.text = []
        self._skip = 0
        self._ld = False
        self._a: list[str] | None = None
        self._script_inline = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        self.tags[tag] = self.tags.get(tag, 0) + 1
        if a.get("style"):
            self.styles_attr += 1
        if tag in self.SKIP:
            self._skip += 1
        if tag == "meta":
            name = (a.get("name") or "").lower()
            if name == "generator":
                self.generator = a.get("content", "")[:80]
            elif name == "viewport":
                self.viewport = a.get("content", "")
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if "icon" in rel:
                self.icon = True
            if "apple-touch-icon" in rel:
                self.touch_icon = True
        elif tag == "script":
            if a.get("src"):
                self.scripts.append(a["src"])
            elif a.get("type", "").lower() == "application/ld+json":
                self._ld = True
            else:
                self._script_inline = True
        elif tag == "img":
            src = a.get("src") or a.get("data-src") or ""
            if src:
                self.img_src.append(src)
            if a.get("loading") == "lazy" or "lazy" in a.get("class", "") or a.get("data-src"):
                self.lazy += 1
            if a.get("srcset"):
                self.srcset += 1
            if re.search(r"\.(webp|avif)(\?|$)", src, re.I):
                self.modern_img += 1
        elif tag == "source" and re.search(r"webp|avif", a.get("type", "") + a.get("srcset", ""), re.I):
            self.modern_img += 1
        elif tag == "form":
            self.forms += 1
        elif tag in ("input", "textarea") and (a.get("type") in ("email", "tel") or tag == "textarea"):
            self.inputs_contact += 1
        elif tag == "a":
            self._a = [a.get("href", ""), ""]

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "script":
            self._ld = False
            self._script_inline = False
        if tag == "a" and self._a is not None:
            self.links.append((self._a[0], self._a[1].strip()[:80]))
            self._a = None

    def handle_data(self, data):
        if self._ld:
            for t in re.findall(r'"@type"\s*:\s*"([^"]+)"', data):
                self.ld_types.append(t)
            return
        if self._script_inline:
            self.inline_js += data[:20000]
            return
        if self._skip:
            return
        if self._a is not None:
            self._a[1] += data
        words = data.split()
        if words:
            self.words += len(words)
            if len(self.text) < 400:
                self.text.append(" ".join(words))


def _year_now() -> int:
    return datetime.now().year


def tech_scan(html: str, headers: dict, url: str, add, facts: dict) -> dict:
    """Nowoczesność, technologia, konwersja i treść strony głównej. Dopisuje problemy przez add()."""
    t = _Tech()
    try:
        t.feed(html)
    except Exception:  # noqa: BLE001 — uszkodzony HTML nie przerywa oceny
        pass
    low = html.lower()
    hdr = {k.lower(): str(v) for k, v in headers.items()}
    tech: dict = {"words": t.words, "h2": t.tags.get("h2", 0), "images": len(t.img_src)}

    # CMS / kreator
    blob = low[:400000] + " " + " ".join(f"{k}:{v}" for k, v in hdr.items()).lower()
    tech["cms"] = next((name for name, marks in BUILDERS if any(m in blob for m in marks)), "")
    if t.generator:
        tech["generator"] = t.generator
        if not tech["cms"]:
            tech["cms"] = t.generator.split()[0]
    tech["page_builder"] = next((n for n, m in PAGE_BUILDERS if m in low), "")
    if tech["cms"] in ("Wix", "Jimdo", "Webnode", "Weebly"):
        add("warn", f"Strona zbudowana w kreatorze {tech['cms']} — ograniczone możliwości SEO i szybkości, "
            "trudno ją rozwijać.", "Technologia")
    m = re.search(r"wordpress\s+([\d.]+)", t.generator, re.I)
    if m:
        tech["wp_version"] = m.group(1)

    # biblioteki
    jq = re.search(r"jquery(?:[.-]|\.min\.js\?ver=|\.js\?ver=)(\d+\.\d+(?:\.\d+)?)", low) or \
        re.search(r"jquery/(\d+\.\d+(?:\.\d+)?)/", low)
    if jq:
        tech["jquery"] = jq.group(1)
        if int(jq.group(1).split(".")[0]) < 3:
            add("warn", f"Stara biblioteka jQuery {jq.group(1)} (sprzed 2016 r.) — znane luki bezpieczeństwa.", "Technologia")
    bs = re.search(r"bootstrap(?:[/@-]|\.min\.css\?ver=|\.css\?ver=)v?(\d)\.(\d+)", low)
    if bs:
        tech["bootstrap"] = f"{bs.group(1)}.{bs.group(2)}"
        if int(bs.group(1)) <= 3:
            add("warn", f"Szablon na Bootstrap {tech['bootstrap']} (lata 2013–2018) — wygląd i kod z poprzedniej epoki.",
                "Nowoczesność")

    # przestarzały HTML
    doctype = html.lstrip()[:120].lower()
    tech["html5"] = doctype.startswith("<!doctype html>") or doctype.startswith("<!doctype html >")
    if doctype.startswith("<!doctype") and not tech["html5"]:
        add("warn", "Stary standard dokumentu (HTML 4 / XHTML) — strona sprzed ery HTML5.", "Nowoczesność")
    old_tags = [tg for tg in ("font", "center", "marquee", "blink", "frameset", "frame") if t.tags.get(tg)]
    if old_tags:
        add("error", "Przestarzały kod HTML (" + ", ".join(f"<{x}>" for x in old_tags) + ") — typowy dla stron "
            "sprzed 2010 r.", "Nowoczesność")
    if ".swf" in low or "shockwave-flash" in low:
        add("error", "Strona używa Flasha — przeglądarki go nie obsługują od 2021 r.", "Nowoczesność")
    semantic = sum(t.tags.get(x, 0) for x in ("header", "nav", "main", "footer", "section", "article"))
    tech["semantic"] = semantic
    if t.tags.get("table", 0) >= 3 and semantic == 0:
        add("warn", "Układ strony oparty na tabelach — technika z lat 2000.", "Nowoczesność")
    elif semantic == 0:
        add("warn", "Brak znaczników HTML5 (header, nav, main, footer) — przestarzała struktura kodu.", "Nowoczesność")

    # rok w stopce
    years = [int(y2 or y1) for y1, y2 in re.findall(
        r"(?:©|&copy;|\(c\)|copyright)\s*(?:by\s*)?(?:[^0-9<]{0,40})?((?:19|20)\d{2})(?:\s*[-–]\s*((?:19|20)\d{2}))?",
        html, re.I)]
    years = [y for y in years if 1995 <= y <= _year_now()]
    if years:
        tech["copyright_year"] = max(years)
        age = _year_now() - max(years)
        if age >= 3:
            add("warn", f"W stopce rok {max(years)} — strona wygląda na nieaktualizowaną od {age} lat.", "Nowoczesność")

    # telefony
    vp = t.viewport.replace(" ", "").lower()
    if vp and "device-width" not in vp and re.search(r"width=\d", vp):
        add("error", "Viewport ze stałą szerokością — na telefonie strona się pomniejsza zamiast dopasować.", "Mobilność")
    if "user-scalable=no" in vp or "maximum-scale=1" in vp:
        add("warn", "Strona blokuje powiększanie na telefonie (utrudnienie dla osób słabowidzących).", "Mobilność")
    if not t.touch_icon:
        add("warn", "Brak ikony dla telefonów (apple-touch-icon) — po dodaniu do ekranu głównego brak logo.", "Marka")

    # obrazy
    if len(t.img_src) >= 6 and not t.modern_img:
        add("warn", "Obrazy tylko w starych formatach (JPG/PNG) — WebP/AVIF są 30–50% lżejsze.", "Wydajność")
    if len(t.img_src) >= 8 and t.lazy == 0:
        add("warn", "Obrazy bez leniwego ładowania (loading=lazy) — telefon pobiera od razu całą stronę.", "Wydajność")
    if len(t.img_src) >= 6 and t.srcset == 0:
        add("warn", "Brak obrazów responsywnych (srcset) — telefon pobiera obrazy w rozmiarze dla komputera.", "Wydajność")
    tech["modern_images"] = t.modern_img
    if t.styles_attr > 80:
        add("warn", f"Dużo stylów wpisanych w kod ({t.styles_attr}) — trudna edycja i niespójny wygląd.", "Nowoczesność")

    # analityka
    js = low + t.inline_js.lower()
    analytics = []
    if re.search(r"gtag/js\?id=g-|['\"]g-[a-z0-9]{6,}['\"]", js):
        analytics.append("Google Analytics 4")
    if "googletagmanager.com/gtm.js" in js or re.search(r"gtm-[a-z0-9]{4,}", js):
        analytics.append("Google Tag Manager")
    ua = bool(re.search(r"ua-\d{4,}-\d+", js))
    if ua:
        analytics.append("Universal Analytics (wyłączony)")
    if "fbq(" in js or "connect.facebook.net" in js:
        analytics.append("Piksel Meta")
    tech["analytics"] = analytics
    if ua and not any(a.startswith(("Google Analytics 4", "Google Tag")) for a in analytics):
        add("error", "Tylko stary Universal Analytics (wyłączony w 2023 r.) — firma nie ma statystyk odwiedzin.", "Analityka")
    elif not analytics:
        add("warn", "Brak Google Analytics / Tag Managera — firma nie mierzy ruchu ani zapytań ze strony.", "Analityka")

    # dane strukturalne
    tech["schema"] = sorted(set(t.ld_types))[:10]
    if not t.ld_types and "itemtype=" not in low:
        add("warn", "Brak danych strukturalnych (schema.org, np. LocalBusiness) — słabsza widoczność w Google i Mapach.", "SEO")

    # konwersja
    hrefs = [h.lower() for h, _ in t.links]
    texts = " ".join(x.lower() for _, x in t.links) + " " + " ".join(t.text).lower()
    tech["phone_link"] = any(h.startswith("tel:") for h in hrefs)
    tech["form"] = t.forms > 0 and t.inputs_contact > 0
    tech["social"] = sorted({name for dom, name in SOCIAL.items() if any(dom in h for h in hrefs)})
    tech["cta"] = sorted({w for w in CTA_WORDS if w in texts})[:6]
    if not tech["phone_link"]:
        add("warn", "Brak klikalnego numeru telefonu (tel:) — na telefonie klient nie zadzwoni jednym dotknięciem.", "Konwersja")
    if not tech["form"] and not any(h.startswith("mailto:") for h in hrefs):
        add("warn", "Na stronie głównej brak formularza i adresu e-mail — trudny pierwszy kontakt.", "Konwersja")
    if not tech["cta"]:
        add("warn", "Brak wyraźnego wezwania do działania (np. „Umów wizytę”, „Zamów wycenę”).", "Konwersja")
    if not tech["social"]:
        add("warn", "Brak linków do mediów społecznościowych.", "Marka")
    if not re.search(r"polityk\w* prywatno|rodo|privacy|cookies", texts + " " + " ".join(hrefs)):
        add("warn", "Brak polityki prywatności / informacji o cookies (wymóg RODO).", "Technologia")
    if not t.icon:
        add("warn", "Brak ikony strony (favicon) — w kartach przeglądarki i w Google bez logo.", "Marka")

    # treść
    if t.words < 250:
        add("warn", f"Mało tekstu na stronie głównej ({t.words} słów) — Google i klienci mają za mało informacji.", "Treść")
    if t.tags.get("h2", 0) == 0:
        add("warn", "Brak śródtytułów H2 — tekst bez struktury, gorzej czytelny dla ludzi i Google.", "Treść")

    # serwer i bezpieczeństwo
    powered = hdr.get("x-powered-by", "")
    pm = re.search(r"php/(\d+)\.(\d+)", powered, re.I)
    if pm:
        tech["php"] = f"{pm.group(1)}.{pm.group(2)}"
        if (int(pm.group(1)), int(pm.group(2))) < (8, 1):
            add("error", f"Serwer działa na PHP {tech['php']} — wersja bez poprawek bezpieczeństwa.", "Technologia")
    missing = [n for h, n in (("strict-transport-security", "HSTS"), ("x-content-type-options", "nosniff"),
                              ("content-security-policy", "CSP")) if h not in hdr]
    if len(missing) >= 2 and url.startswith("https"):
        add("warn", f"Brak nagłówków bezpieczeństwa ({', '.join(missing)}).", "Technologia")
    if url.startswith("https") and re.search(r"""(?:src|href)=["']http://(?!localhost)""", low):
        add("warn", "Mieszana zawartość: elementy ładowane przez http:// na stronie https.", "Technologia")
    facts["tech"] = tech
    facts["text_sample"] = " ".join(t.text)[:1500]
    return tech


def crawl_pages(url: str, links: list[str], add, facts: dict, limit: int = 8) -> list[dict]:
    """Do `limit` podstron (najpierw kontakt, oferta, o nas…): tytuły, opisy, H1 i ilość tekstu."""
    from . import monitor
    host = urllib.parse.urlsplit(url).netloc
    seen, cand = {url.split("#")[0].rstrip("/")}, []
    for href in links:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urllib.parse.urljoin(url, href).split("#")[0]
        p = urllib.parse.urlsplit(full)
        if p.netloc != host or full.rstrip("/") in seen or re.search(r"\.(pdf|jpe?g|png|gif|webp|zip|docx?)$", p.path, re.I):
            continue
        seen.add(full.rstrip("/"))
        cand.append(full)
    cand.sort(key=lambda u: not any(w in u.lower() for w in PRIORITY_PAGES))
    pages = []
    for full in cand[:limit]:
        try:
            r = monitor.fetch(full, limit=1_500_000, timeout=20)
        except (OSError, urllib.error.URLError, ValueError):
            continue
        if r.code >= 400 or "html" not in r.headers.get("content-type", "html"):
            continue
        doc, tx = monitor._Collect(), _Tech()
        try:
            doc.feed(r.text)
            tx.feed(r.text)
        except Exception:  # noqa: BLE001
            pass
        pages.append({"path": urllib.parse.urlsplit(r.final_url).path or "/", "title": doc.title.strip()[:120],
                      "description": len(doc.meta.get("description", "")), "h1": len(doc.h1), "words": tx.words})
    facts["pages"] = pages
    facts["pages_found"] = len(cand)
    if not pages:
        return pages
    titles: dict[str, list[str]] = {}
    for p in pages:
        titles.setdefault(p["title"].lower(), []).append(p["path"])
    dup = [v for k, v in titles.items() if k and len(v) > 1]
    if dup:
        add("warn", f"Podstrony z tym samym tytułem ({sum(len(v) for v in dup)}): " + ", ".join(dup[0][:4])
            + " — Google nie odróżnia ich od siebie.", "Podstrony")
    no_desc = [p["path"] for p in pages if not p["description"]]
    if no_desc:
        add("warn", f"{len(no_desc)} z {len(pages)} sprawdzonych podstron bez opisu meta: " + ", ".join(no_desc[:4]) + ".", "Podstrony")
    no_h1 = [p["path"] for p in pages if not p["h1"]]
    if no_h1:
        add("warn", f"{len(no_h1)} podstron bez nagłówka H1: " + ", ".join(no_h1[:4]) + ".", "Podstrony")
    thin = [p["path"] for p in pages if p["words"] < 150]
    if len(thin) >= max(2, len(pages) // 2):
        add("warn", f"Większość podstron ma bardzo mało tekstu (np. {', '.join(thin[:3])}).", "Treść")
    if not any(any(w in p["path"].lower() for w in ("kontakt", "contact")) for p in pages) and \
            not any(any(w in u.lower() for w in ("kontakt", "contact")) for u in cand):
        add("warn", "Nie znaleziono podstrony „Kontakt”.", "Konwersja")
    return pages


# ------------------------------------------------------------------ zrzuty ekranu (kontener „zrzuty”)

SHOTS = {"komputer": (1440, 900), "telefon": (390, 844)}


def _multipart(fields: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = b""
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    return out + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def screenshot(url: str, width: int, height: int, base: str, timeout: float = 120) -> bytes:
    body, ctype = _multipart({"url": url, "width": width, "height": height, "clip": "true", "format": "jpeg",
                              "quality": 78, "waitDelay": "3s", "optimizeForSpeed": "true"})
    req = urllib.request.Request(base.rstrip("/") + "/forms/chromium/screenshot/url", data=body,
                                 headers={"Content-Type": ctype}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — adres z konfiguracji
        data = r.read(15 * 1024 * 1024)
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("odpowiedź nie jest obrazem")
    return data


def shot_path(site_id: int, kind: str) -> Path | None:
    d = CONFIG.get("dir")
    return Path(d) / f"{site_id}-{kind}.jpg" if d else None


def take_screenshots(site_id: int, url: str) -> dict[str, bytes]:
    """Zrzuty strony (komputer + telefon) zapisane na dysku. Pusty słownik, gdy usługa zrzutów nie działa."""
    base = CONFIG.get("screenshot_url") or ""
    if not base:
        return {}
    out = {}
    for kind, (w, h) in SHOTS.items():
        try:
            out[kind] = screenshot(url, w, h, base)
        except (OSError, urllib.error.URLError, ValueError):
            continue
        p = shot_path(site_id, kind)
        if p:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(out[kind])
    return out


# ------------------------------------------------------------------ podgląd udostępniania (Open Graph) i skrypty

IMAGE_TYPES = {b"\xff\xd8": "image/jpeg", b"\x89P": "image/png", b"GI": "image/gif", b"RI": "image/webp"}


def image_type(data: bytes) -> str:
    t = IMAGE_TYPES.get(data[:2], "")
    return "" if t == "image/webp" and data[8:12] != b"WEBP" else t


def og_image(site_id: int, facts: dict, add) -> dict | None:
    """Pobiera obrazek podglądu (og:image / twitter:image) do panelu i sprawdza jego wymiary (zalecane 1200×630)."""
    from . import monitor
    og = facts.get("og") or {}
    path = shot_path(site_id, "og")
    raw = og.get("og:image") or og.get("twitter:image") or ""
    if path and path.exists():
        path.unlink()
    if not raw:
        return None
    url = urllib.parse.urljoin(facts.get("final_url") or facts.get("url") or "", raw)
    info: dict = {"url": url[:400], "ok": False}
    facts["og_image"] = info
    if not url.startswith(("http://", "https://")):
        add("warn", "Adres obrazka og:image jest nieprawidłowy — podgląd przy udostępnianiu będzie bez zdjęcia.", "SEO")
        return info
    try:
        r = monitor.fetch(url, limit=8 * 1024 * 1024, timeout=20)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        add("warn", f"Obrazek podglądu (og:image) nie wczytuje się: {monitor._err(exc)}.", "SEO")
        return info
    kind = image_type(r.body)
    if r.code >= 400 or not kind:
        add("warn", f"Obrazek podglądu (og:image) nie wczytuje się (HTTP {r.code}).", "SEO")
        return info
    info.update(ok=True, kb=len(r.body) // 1024, type=kind)
    try:
        from io import BytesIO

        from PIL import Image
        with Image.open(BytesIO(r.body)) as im:
            info["w"], info["h"] = im.size
    except Exception:  # noqa: BLE001 — brak Pillow albo nietypowy plik: bez wymiarów
        pass
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.body)
    if info.get("w") and (info["w"] < 600 or info["h"] < 315):
        add("warn", f"Obrazek podglądu ma tylko {info['w']}×{info['h']} px — Facebook pokaże go jako małą miniaturę "
            "(zalecane 1200×630).", "SEO")
    if info["kb"] > 1024:
        add("warn", f"Obrazek podglądu waży {info['kb']} KB — przy udostępnianiu może się nie wczytać (zalecane < 1 MB).", "SEO")
    return info


def script_sizes(srcs: list[str], page_url: str, limit: int = 25) -> list[dict]:
    """Bez testu PageSpeed: rozmiary plików JavaScript (bez czasu wykonania), od największego."""
    from . import gsc, monitor
    host = urllib.parse.urlsplit(page_url).netloc
    out = []
    for src in list(dict.fromkeys(srcs))[:limit]:
        if not src.startswith(("http://", "https://")):
            continue
        try:
            r = monitor.fetch(src, limit=6 * 1024 * 1024, timeout=15)
        except (OSError, urllib.error.URLError, ValueError):
            continue
        if r.code < 400:
            out.append({"url": gsc._short(src), "kb": round(len(r.body) / 1024),
                        "third": urllib.parse.urlsplit(src).netloc != host})
    return sorted(out, key=lambda x: -x["kb"])[:15]


# ------------------------------------------------------------------ ocena wyglądu (model AI z obrazami)

VISION_PROMPT = """Jesteś doświadczonym projektantem stron WWW i specjalistą od wizerunku marki w agencji BSK Media.
Pierwszy obraz to strona {url} na komputerze (górna część), drugi — ta sama strona na telefonie.

Oceń szczerze i konkretnie WYGLĄD strony, tak jak oceniłby go klient firmy:
- czy wygląda nowocześnie, czy jak strona sprzed lat (podaj szacowany rok, w którym mogła powstać),
- estetyka: kolory, typografia, zdjęcia (stockowe / własne / słabej jakości), odstępy, spójność,
- czytelność i hierarchia: czy w 5 sekund wiadomo, czym zajmuje się firma i co zrobić dalej,
- wersja na telefon: czy jest dopasowana i wygodna,
- marka: czy logo i identyfikacja wyglądają profesjonalnie, czy firma potrzebuje rebrandingu.

Odpowiedz wyłącznie obiektem JSON:
{{"wyglad": liczba 0-10, "nowoczesnosc": liczba 0-10, "telefon": liczba 0-10, "marka": liczba 0-10,
"szacowany_rok": "np. ok. 2014", "przestarzala": true/false, "rebranding": true/false,
"mocne_strony": ["…"], "slabe_strony": ["…", "…"], "opis": "3-5 zdań po polsku o wyglądzie strony"}}"""


def _json_obj(text: str | None) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def vision_review(cfg: ai.Config, url: str, shots: dict[str, bytes]) -> dict | None:
    if not shots:
        return None
    imgs = [shots[k] for k in ("komputer", "telefon") if k in shots]
    return _json_obj(ai.generate(cfg, VISION_PROMPT.format(url=url), images=imgs))


# ------------------------------------------------------------------ ocena i rekomendacja

REVIEW_PROMPT = """Jesteś szefem zespołu w agencji BSK Media (strony WWW, sklepy, SEO, kampanie, identyfikacja
wizualna). Przygotuj dla właściciela agencji SZCZEGÓŁOWĄ ocenę strony {kind_text} i rekomendację, co tej firmie
sprzedać / zaproponować. Bądź konkretny, opieraj się wyłącznie na danych poniżej, nie wymyślaj faktów.

Firma: {name}{source}
Strona: {url}
Wynik audytu technicznego: {score}/100
Technologia: {tech}
Podstrony (ścieżka, tytuł, słowa): {pages}
Fragment tekstu ze strony: „{text}”
Ocena wyglądu ze zrzutów ekranu: {visual}
{integ}
Problemy wykryte automatycznie (obszar: opis):
{issues}

Wstępne oceny obszarów wyliczone automatycznie (0-10): {pre}

Odpowiedz wyłącznie obiektem JSON (teksty po polsku):
{{"oceny": {{"seo": 0-10, "wydajnosc": 0-10, "wyglad": 0-10, "mobilnosc": 0-10, "tresc": 0-10,
  "konwersja": 0-10, "technologia": 0-10, "marka": 0-10}},
"werdykt": jedno z "przebudowa", "odswiezenie", "optymalizacja", "poprawki", "dobra",
"werdykt_uzasadnienie": "2-3 zdania: dlaczego taki werdykt",
"szacowany_wiek": "np. strona z ok. 2016 r.",
"rebranding": true/false, "rebranding_uzasadnienie": "1 zdanie",
"uslugi": [{{"usluga": jedna z {services}, "priorytet": "wysoki"/"średni"/"niski", "dlaczego": "1-2 zdania"}}],
"podsumowanie": "4-6 zdań dla właściciela agencji: stan strony i największa szansa sprzedażowa",
"argumenty": ["3-5 konkretnych argumentów do rozmowy z firmą, z liczbami z audytu"],
"szybkie_poprawki": ["3-6 konkretnych poprawek, np. lepszy tytuł strony"],
"potencjal": 0-100 (jak bardzo ta firma potrzebuje naszych usług i jak łatwo z nią porozmawiać),
"potencjal_uzasadnienie": "1-2 zdania"}}
Zasady werdyktu: „przebudowa” gdy strona jest przestarzała wizualnie lub technicznie (stary kod, brak wersji
na telefon, kreator, wygląd sprzed lat); „odswiezenie” gdy technika jest w porządku, ale wygląd odstaje;
„optymalizacja” gdy wygląd jest dobry, a kuleje SEO / szybkość / konwersja; „poprawki” przy drobnych sprawach."""


def area_scores(issues: list[dict]) -> dict[str, int]:
    out = {k: 10 for k, _ in AREAS}
    for i in issues:
        k = AREA_OF.get(i.get("area", ""), "technologia")
        out[k] -= 3 if i["level"] == "error" else 1
    return {k: max(0, v) for k, v in out.items()}


def heuristic(result: dict, site=None, visual: dict | None = None) -> dict:
    """Ocena bez AI: oceny obszarów z problemów, werdykt z sygnałów przestarzałości."""
    issues = result.get("issues", [])
    f = result.get("facts", {})
    tech = f.get("tech", {})
    sc = area_scores(issues)
    if visual:
        for k_vis, k in (("wyglad", "wyglad"), ("telefon", "mobilnosc"), ("marka", "marka")):
            if isinstance(visual.get(k_vis), (int, float)):
                sc[k] = round((sc[k] + max(0, min(10, visual[k_vis]))) / 2)
    old = sum(1 for i in issues if i.get("area") == "Nowoczesność")
    old += 2 if any("Flash" in i["text"] or "Viewport ze stałą" in i["text"] or "znacznika viewport" in i["text"]
                    for i in issues) else 0
    old += 1 if tech.get("cms") in ("Wix", "Jimdo", "Webnode", "Weebly") else 0
    old += 2 if visual and visual.get("przestarzala") else 0
    if old >= 3 or sc["wyglad"] <= 3:
        verdict = "przebudowa"
    elif old >= 1 or sc["wyglad"] <= 6:
        verdict = "odswiezenie"
    elif min(sc["seo"], sc["wydajnosc"], sc["konwersja"]) <= 6:
        verdict = "optymalizacja"
    elif issues:
        verdict = "poprawki"
    else:
        verdict = "dobra"
    services = []

    def svc(name, prio, why):
        services.append({"usluga": name, "priorytet": prio, "dlaczego": why})
    if verdict == "przebudowa":
        svc("Nowa strona internetowa", "wysoki", "Strona jest przestarzała wizualnie lub technicznie.")
    elif verdict == "odswiezenie":
        svc("Odświeżenie wyglądu strony", "wysoki", "Wygląd lub kod odstaje od współczesnych stron.")
    if sc["seo"] <= 7:
        svc("Pozycjonowanie SEO", "wysoki" if sc["seo"] <= 4 else "średni", "Braki w podstawach SEO (tytuły, opisy, struktura).")
    if sc["wydajnosc"] <= 7 and verdict != "przebudowa":
        svc("Optymalizacja szybkości", "średni", "Strona ładuje się wolniej, niż powinna.")
    if sc["konwersja"] <= 7:
        svc("Analityka i pomiar konwersji" if not tech.get("analytics") else "Teksty i treści na stronę", "średni",
            "Brakuje elementów, które zamieniają odwiedzających w klientów.")
    if visual and visual.get("rebranding"):
        svc("Rebranding (logo i identyfikacja)", "średni", "Identyfikacja wizualna wygląda na przestarzałą.")
    if sc["technologia"] <= 6:
        svc("Opieka techniczna i aktualizacje", "średni", "Przestarzałe oprogramowanie lub braki bezpieczeństwa.")
    top = [i["text"] for i in sorted(issues, key=lambda i: i["level"] != "error")][:5]
    contact = bool(site and (site["contact_email"] or site["phone"])) if site is not None else False
    need = {"przebudowa": 80, "odswiezenie": 65, "optymalizacja": 50, "poprawki": 30, "dobra": 10}[verdict]
    return {
        "oceny": sc, "werdykt": verdict,
        "werdykt_uzasadnienie": f"Wyliczone automatycznie z {len(issues)} wykrytych problemów"
                                f"{' i oceny wyglądu' if visual else ''}.",
        "szacowany_wiek": (visual or {}).get("szacowany_rok") or (
            f"stopka z {tech['copyright_year']} r." if tech.get("copyright_year") else ""),
        "rebranding": bool(visual and visual.get("rebranding")), "rebranding_uzasadnienie": "",
        "uslugi": services, "podsumowanie": "", "argumenty": top[:4], "szybkie_poprawki": top[:5],
        "potencjal": min(100, need + (10 if contact else 0)), "potencjal_uzasadnienie": "",
        "by_ai": False,
    }


def _clean(review: dict, base: dict) -> dict:
    """Uzupełnia i normalizuje odpowiedź modelu wartościami wyliczonymi automatycznie."""
    out = dict(base)
    sc = dict(base["oceny"])
    for k, v in (review.get("oceny") or {}).items():
        if k in sc and isinstance(v, (int, float)):
            sc[k] = max(0, min(10, round(v)))
    out["oceny"] = sc
    if review.get("werdykt") in VERDICTS:
        out["werdykt"] = review["werdykt"]
    for k in ("werdykt_uzasadnienie", "szacowany_wiek", "rebranding_uzasadnienie", "podsumowanie", "potencjal_uzasadnienie"):
        if isinstance(review.get(k), str) and review[k].strip():
            out[k] = review[k].strip()[:1500]
    if isinstance(review.get("rebranding"), bool):
        out["rebranding"] = review["rebranding"]
    svcs = [s for s in review.get("uslugi") or [] if isinstance(s, dict) and s.get("usluga")]
    if svcs:
        out["uslugi"] = [{"usluga": str(s["usluga"])[:80], "priorytet": str(s.get("priorytet", "średni"))[:10],
                          "dlaczego": str(s.get("dlaczego", ""))[:400]} for s in svcs[:8]]
    for k in ("argumenty", "szybkie_poprawki"):
        items = [str(x)[:400] for x in review.get(k) or [] if str(x).strip()]
        if items:
            out[k] = items[:8]
    try:
        out["potencjal"] = max(0, min(100, int(review.get("potencjal", out["potencjal"]))))
    except (TypeError, ValueError):
        pass
    out["by_ai"] = True
    return out


def build_review(cfg: ai.Config | None, site, result: dict, visual: dict | None, progress=lambda t: None) -> dict:
    base = heuristic(result, site, visual)
    if visual:
        base["wyglad_ai"] = visual
    if not cfg:
        return base
    f = result.get("facts", {})
    tech = f.get("tech", {})
    tech_txt = ", ".join(f"{k}: {v}" for k, v in tech.items() if v not in ("", [], None, 0)) or "brak danych"
    pages = "; ".join(f"{p['path']} „{p['title'][:50]}” {p['words']} sł." for p in f.get("pages", [])[:8]) or "nie sprawdzono"
    integ = ""
    try:
        it = json.loads(site["integ"] or "{}") if site is not None and "integ" in site.keys() else {}
    except (ValueError, TypeError):
        it = {}
    if it.get("gsc") and not it["gsc"].get("error"):
        g = it["gsc"]
        integ += f"Google Search Console (7 dni): kliknięcia {g['current']['clicks']} (poprzednio {g['previous']['clicks']}), " \
                 f"średnia pozycja {g['current']['position']}.\n"
    if it.get("ga") and not it["ga"].get("error"):
        g = it["ga"]
        integ += (f"Google Analytics (7 dni): {g['current']['sessions']} sesji (poprzednio {g['previous']['sessions']}), "
                  f"{g['current']['users']} użytkowników, kluczowe zdarzenia {g['current']['key_events']} "
                  f"(poprzednio {g['previous']['key_events']}), zaangażowanie {g['current']['engagement']}%. Kanały: "
                  + ", ".join(f"{c['name']} {c['value']}" for c in g.get("channels", [])[:5]) + ".\n")
    psi = f.get("psi") or (it.get("psi") if it.get("psi") and not it["psi"].get("error") else None)
    if psi:
        sc = psi.get("scripts") or {}
        integ += (f"PageSpeed (telefon): wydajność {psi.get('performance')}/100, LCP {psi.get('lcp')}, blokowanie "
                  f"{psi.get('tbt', '')}. Najdłużej wykonujące się skrypty: "
                  + (", ".join(f"{b['url']} {b['total']} ms" for b in sc.get("bootup", [])[:4]) or "brak danych") + ".\n")
    kind = site["kind"] if site is not None else "lead"
    prompt = REVIEW_PROMPT.format(
        kind_text="potencjalnego klienta (firmy, z którą jeszcze nie współpracujemy)" if kind == "lead"
        else "naszego klienta (prowadzimy jego stronę)",
        name=(site["name"] if site is not None else "") or "—",
        source=f" ({site['source']})" if site is not None and site["source"] else "",
        url=f.get("final_url") or (site["url"] if site is not None else ""), score=result.get("score", "?"),
        tech=tech_txt, pages=pages, text=(f.get("text_sample") or "")[:900],
        visual=json.dumps(visual, ensure_ascii=False)[:1500] if visual else "brak (usługa zrzutów albo model bez obsługi obrazów)",
        integ=integ, issues="\n".join(f"- {i['area']}: {i['text']}" for i in result.get("issues", [])[:45]) or "- brak",
        pre=json.dumps(base["oceny"], ensure_ascii=False), services=", ".join(f"„{s}”" for s in SERVICES))
    progress(f"{(site['name'] or site['url']) if site is not None else ''}: model AI przygotowuje pełną ocenę i rekomendację")
    data = _json_obj(ai.generate(cfg, prompt))
    return _clean(data, base) if data else base


def as_text(rv: dict) -> str:
    """Ocena jako tekst (do e-maili, raportu i starszych widoków)."""
    lines = [f"Werdykt: {VERDICTS.get(rv.get('werdykt'), '')}" + (f" — {rv['werdykt_uzasadnienie']}" if rv.get("werdykt_uzasadnienie") else "")]
    if rv.get("szacowany_wiek"):
        lines.append(f"Wiek strony: {rv['szacowany_wiek']}")
    if rv.get("podsumowanie"):
        lines.append(rv["podsumowanie"])
    if rv.get("uslugi"):
        lines.append("Proponowane usługi:\n" + "\n".join(f"- {s['usluga']} ({s['priorytet']}): {s['dlaczego']}" for s in rv["uslugi"]))
    if rv.get("szybkie_poprawki"):
        lines.append("Szybkie poprawki:\n" + "\n".join(f"- {x}" for x in rv["szybkie_poprawki"]))
    return "\n\n".join(lines)
