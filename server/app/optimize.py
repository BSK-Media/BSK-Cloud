"""Plan optymalizacji strony: konkretne problemy z PageSpeed, Network i audytu -> rozwiązania z gotowym kodem.

Każde zadanie: problem z danymi (pliki, czasy, rozmiary), priorytet, spodziewany zysk, kroki i fragmenty kodu
dopasowane do strony (WordPress / Elementor / WooCommerce: PHP do wtyczki „Code Snippets”; inne strony: HTML,
.htaccess). Kod jest zachowawczy: każdy fragment da się wyłączyć jednym kliknięciem w Code Snippets.
"""
from __future__ import annotations

import re
import urllib.parse

PRIO = {"wysoki": 0, "średni": 1, "niski": 2}


def _q(s: str) -> str:
    """Tekst w apostrofach PHP."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _file(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return (p.path.rsplit("/", 1)[-1] or p.netloc) if p.netloc else url


def _sec(ms: float) -> str:
    return f"{ms / 1000:.1f} s".replace(".", ",")


def _opp(psi: dict, *ids: str) -> dict | None:
    for f in psi.get("opportunities", []) + psi.get("bp_findings", []):
        if f.get("id") in ids:
            return f
    return None


def detect(facts: dict, psi: dict) -> dict:
    urls = " ".join(r["url"] for r in (psi.get("network") or {}).get("rows", [])).lower()
    tech = facts.get("tech") or {}
    cms = (tech.get("cms") or "").lower()
    wp = "wordpress" in cms or "/wp-content/" in urls or "/wp-includes/" in urls
    return {"wp": wp, "elementor": "elementor" in urls or tech.get("page_builder") == "Elementor",
            "woo": "woocommerce" in urls, "divi": "et_pb" in urls or tech.get("page_builder") == "Divi",
            "litespeed": "litespeed" in urls, "rocket": "wp-rocket" in urls}


GENERIC = {"style", "styles", "main", "theme", "app", "frontend", "custom", "global", "bootstrap", "common", "base", "site"}


def build_plan(facts: dict, psi: dict | None, page_url: str = "") -> dict:
    """Zadania od najważniejszych. psi = pełny wynik speed.parse (telefon) albo None."""
    psi = psi or {}
    env = detect(facts, psi)
    net = psi.get("network") or {}
    rows = net.get("rows", [])
    sc = psi.get("scripts") or {}
    metrics = {m["id"]: m for m in psi.get("metrics", [])}
    tasks: list[dict] = []
    page = facts.get("final_url") or facts.get("url") or page_url

    def task(key, title, prio, area, problem, steps, snippets=(), gain="", files=()):
        tasks.append({"key": key, "title": title, "prio": prio, "area": area, "problem": problem, "steps": list(steps),
                      "snippets": [s for s in snippets if s], "gain": gain, "files": list(files)[:12]})

    def php(title, code, note=""):
        return {"title": title, "lang": "php", "code": code.strip() + "\n",
                "where": "WordPress → wtyczka Code Snippets → Dodaj nowy → wklej → „Uruchamiaj wszędzie” → Zapisz i aktywuj"
                         + (f". {note}" if note else "")}

    def html(title, code, where):
        return {"title": title, "lang": "html", "code": code.strip() + "\n", "where": where}

    # 1. Element głównej treści (LCP)
    lcp = metrics.get("largest-contentful-paint")
    el = psi.get("lcp_element") or {}
    if lcp and lcp["rating"] != "good":
        img = re.search(r'src="([^"]+)"', el.get("snippet", ""))
        phases = {p["phase"]: p["ms"] for p in el.get("phases", [])}
        delay = phases.get("Opóźnienie startu pobierania", 0)
        src = urllib.parse.urljoin(page, img.group(1)) if img else ""
        lazy = "loading=\"lazy\"" in el.get("snippet", "") or "lazyload" in el.get("snippet", "")
        steps = [f"Element głównej treści: {el.get('selector') or 'nie rozpoznano'} — pojawia się po {lcp['display']} (dobrze < 2,5 s)."]
        if phases:
            steps.append("Fazy: " + ", ".join(f"{k} {v} ms" for k, v in phases.items()) + ". "
                         + ("Największa jest „opóźnienie startu pobierania” — przeglądarka za późno dowiaduje się o obrazku: "
                            "trzeba go wczytać z wyprzedzeniem (preload)." if delay and delay == max(phases.values()) else
                            "Skróć największą fazę."))
        if lazy:
            steps.append("Główny obrazek ma leniwe ładowanie (lazy) — dla pierwszego ekranu trzeba je wyłączyć.")
        steps += ["Obrazek pierwszego ekranu: WebP/AVIF, szerokość max ok. 1600 px (telefon: osobna wersja ~800 px), < 200 KB.",
                  "Nie ukrywaj go w sliderze ani animacji wejścia (Elementor: wyłącz „Animacja wejścia” dla tej sekcji)."]
        snippets = []
        if src and env["wp"]:
            snippets.append(php("Wczytaj główny obrazek z wyprzedzeniem (tylko strona główna)", f"""
// BSK: preload obrazka głównej treści (LCP) na stronie głównej
add_action('wp_head', function () {{
    if (is_front_page()) {{
        echo '<link rel="preload" as="image" href="' . esc_url({_q(src)}) . '" fetchpriority="high">' . "\\n";
    }}
}}, 1);
"""))
            snippets.append(php("Nie stosuj leniwego ładowania do pierwszych obrazków", """
// BSK: pierwsze 2 obrazki strony bez loading="lazy" (WordPress 5.9+)
add_filter('wp_omit_loading_attr_threshold', function () {
    return 2;
});
"""))
        elif src:
            snippets.append(html("Preload głównego obrazka", f'<link rel="preload" as="image" href="{src}" fetchpriority="high">',
                                 "W sekcji <head> strony głównej, jak najwyżej."))
        task("lcp", "Przyspiesz pojawienie się głównej treści (LCP)", "wysoki", "Szybkość",
             f"Główna treść pojawia się po {lcp['display']}" + (f" — {el.get('selector')}" if el.get("selector") else "") + ".",
             steps, snippets, gain=f"LCP {lcp['display']} → cel < 2,5 s", files=[src] if src else [])

    # 2. Pliki blokujące wyświetlenie
    rb = sc.get("render_blocking") or []
    blocking_rows = [r for r in rows if r.get("blocking")]
    if rb or blocking_rows:
        total = sum(b["ms"] for b in rb)
        css = [r for r in blocking_rows if r["type"] == "CSS"] or [b for b in rb if ".css" in b["url"]]
        js = [r for r in blocking_rows if r["type"] == "JS"] or [b for b in rb if ".js" in b["url"]]
        names = [_file(r.get("url", "")) for r in (css + js)]
        parts = sorted({p for p in (re.sub(r"(\.min)?\.(css|js).*$", "", n) for n in names if n)
                        if p and p.lower() not in GENERIC and len(p) >= 4})[:25]
        snippets = []
        if env["wp"] and js:
            snippets.append(php("Skrypty z atrybutem defer (nie blokują wyświetlenia)", """
// BSK: defer dla skryptów w części publicznej (jQuery zostaje bez zmian — wiele wtyczek go wymaga)
add_filter('script_loader_tag', function ($tag, $handle, $src) {
    if (is_admin() || wp_doing_ajax() || strpos($tag, ' defer') !== false || strpos($tag, ' async') !== false) {
        return $tag;
    }
    $skip = array('jquery', 'jquery-core', 'jquery-migrate');
    if (in_array($handle, $skip, true)) {
        return $tag;
    }
    return str_replace(' src=', ' defer src=', $tag);
}, 10, 3);
""", "Po włączeniu sprawdź stronę (menu, slider, koszyk). Gdy coś nie działa — dopisz uchwyt skryptu do $skip."))
        if env["wp"] and css:
            lst = ", ".join(_q(p) for p in parts) or "/* np. 'swiper', 'animations', 'lightbox' */"
            snippets.append(php("Niekrytyczne style CSS wczytywane asynchronicznie", f"""
// BSK: wybrane arkusze CSS nie blokują pierwszego wyświetlenia (ładowane po wczytaniu strony)
add_filter('style_loader_tag', function ($html, $handle, $href) {{
    if (is_admin()) {{
        return $html;
    }}
    // fragmenty nazw plików z raportu — zostaw tylko te, które nie dotyczą pierwszego ekranu
    $async = array({lst});
    foreach ($async as $part) {{
        if (strpos($href, $part) !== false) {{
            $lazy = preg_replace('/media=([\\'"])all\\1/', 'media="print" onload="this.media=\\'all\\'"', $html);
            return $lazy . '<noscript>' . $html . '</noscript>';
        }}
    }}
    return $html;
}}, 10, 3);
""", "Usuń z listy pliki odpowiedzialne za nagłówek i pierwszy ekran (np. style motywu), żeby strona nie „mrugała”."))
        steps = [f"Pliki wstrzymujące pokazanie strony: {len(rb) or len(blocking_rows)}" + (f", łącznie ok. {_sec(total)}." if total else "."),
                 "Skrypty: atrybut defer (kod poniżej) albo w wtyczce cache: „Opóźnij / odrocz JavaScript”.",
                 "Style: krytyczny CSS w <head>, reszta asynchronicznie (LiteSpeed Cache / WP Rocket: „Usuń nieużywany CSS”, "
                 "„Krytyczny CSS”) albo kod poniżej dla wybranych plików."]
        if env["elementor"]:
            steps.append("Elementor → Ustawienia → Funkcje: włącz „Zoptymalizowane ładowanie zasobów”, „Ulepszone ładowanie CSS”, "
                         "„Wczytywanie ikon czcionek inline”.")
        task("render-blocking", "Usuń pliki blokujące wyświetlenie strony", "wysoki", "Szybkość",
             f"Blokują: {', '.join(names[:8])}" + (" …" if len(names) > 8 else "") + ".", steps, snippets,
             gain=f"do ok. {_sec(total)} szybciej" if total else "", files=[r.get("url", "") for r in (css + js)])

    # 3. Skrypty zewnętrzne (Tag Manager, piksele, czaty)
    third = [t for t in sc.get("third_party") or [] if t["blocking"] >= 50 or t["main"] >= 250]
    if third:
        gtm = next((re.search(r"id=(GTM-[A-Z0-9]+)", r["url"]) for r in rows if "googletagmanager.com/gtm.js" in r["url"]), None)
        gtm_id = gtm.group(1) if gtm else "GTM-XXXXXXX"
        snippets = [php("Google Tag Manager wczytywany po pierwszej interakcji (albo po 4 s)", f"""
// BSK: opóźnione wczytanie Google Tag Manager — statystyki zostają, strona przestaje „zamarzać”
// WAŻNE: usuń dotychczasowy kod GTM (motyw / Site Kit / wtyczka), żeby nie był wczytywany dwa razy.
add_action('wp_head', function () {{
    if (is_admin() || is_user_logged_in()) {{
        return;
    }}
    ?>
<script>
(function () {{
  var done = false;
  function loadGtm() {{
    if (done) return; done = true;
    window.dataLayer = window.dataLayer || [];
    window.dataLayer.push({{'gtm.start': new Date().getTime(), event: 'gtm.js'}});
    var s = document.createElement('script'); s.async = true;
    s.src = 'https://www.googletagmanager.com/gtm.js?id={gtm_id}';
    document.head.appendChild(s);
  }}
  ['scroll', 'mousemove', 'touchstart', 'keydown', 'click'].forEach(function (e) {{
    window.addEventListener(e, loadGtm, {{once: true, passive: true}});
  }});
  setTimeout(loadGtm, 4000);
}})();
</script>
    <?php
}}, 20);
""", "Zachowaj osobno część <noscript> GTM po <body>, jeśli była.")] if env["wp"] else [
            html("Google Tag Manager po interakcji", f"""<script>
(function(){{var d=false;function g(){{if(d)return;d=true;window.dataLayer=window.dataLayer||[];
window.dataLayer.push({{'gtm.start':new Date().getTime(),event:'gtm.js'}});var s=document.createElement('script');
s.async=true;s.src='https://www.googletagmanager.com/gtm.js?id={gtm_id}';document.head.appendChild(s);}}
['scroll','mousemove','touchstart','keydown','click'].forEach(function(e){{addEventListener(e,g,{{once:true,passive:true}});}});
setTimeout(g,4000);}})();
</script>""", "Zamiast obecnego kodu GTM w <head>.")]
        steps = [", ".join(f"{t['entity']}: blokuje {t['blocking']} ms, procesor {t['main']} ms, {t['kb']} KB" for t in third[:5]) + ".",
                 "Usuń nieużywane tagi i piksele w Tag Managerze (stare kampanie, zdublowane GA, nieużywane piksele).",
                 "Czat, piksel Facebooka, Hotjar itp. — ładuj przez Tag Managera z wyzwalaczem „Okno wczytane” albo po interakcji.",
                 "Wtyczki cache z funkcją „Opóźnij JavaScript” (WP Rocket, LiteSpeed Cache, Perfmatters, Flying Scripts) zrobią to "
                 "bez kodu — wpisz tam: googletagmanager, fbevents, gtag."]
        task("third-party", "Opóźnij skrypty zewnętrzne", "wysoki" if sum(t["blocking"] for t in third) >= 250 else "średni",
             "Skrypty", f"Usługi zewnętrzne blokują stronę łącznie {sum(t['blocking'] for t in third)} ms.", steps, snippets,
             gain=f"blokowanie −{sum(t['blocking'] for t in third)} ms")

    # 4. Najdłużej wykonujące się / nieużywany JavaScript
    slow = [b for b in sc.get("bootup") or [] if b["total"] >= 300]
    unused = [h for h in sc.get("heavy") or [] if h["unused_kb"] >= 20]
    if slow or unused:
        snippets = []
        if env["wp"]:
            snippets.append(php("Wyłącz zbędne skrypty WordPressa (emoji, embed, dashicons dla gości)", """
// BSK: mniej zbędnego kodu w części publicznej
remove_action('wp_head', 'print_emoji_detection_script', 7);
remove_action('wp_print_styles', 'print_emoji_styles');
remove_action('wp_head', 'wp_oembed_add_host_js');
add_action('wp_enqueue_scripts', function () {
    if (!is_user_logged_in()) {
        wp_deregister_style('dashicons');
    }
    wp_dequeue_script('wp-embed');
}, 100);
"""))
        if env["woo"]:
            snippets.append(php("WooCommerce: cart-fragments tylko w sklepie, koszyku i zamówieniu", """
// BSK: odświeżanie mini-koszyka (admin-ajax) tylko tam, gdzie jest potrzebne
add_action('wp_enqueue_scripts', function () {
    if (function_exists('is_woocommerce') && !is_woocommerce() && !is_cart() && !is_checkout()) {
        wp_dequeue_script('wc-cart-fragments');
    }
}, 99);
""", "Jeśli w nagłówku jest licznik koszyka (np. Elementor Menu Cart), sprawdź, czy po dodaniu produktu nadal się odświeża."))
        steps = []
        if slow:
            steps.append("Najdłużej wykonują się: " + "; ".join(f"{b['url']} — {b['total']} ms" for b in slow[:5]) + ".")
        if unused:
            steps.append("Pobierany, ale nieużywany kod: " + ", ".join(f"{h['url'].split('/')[-1]} ({h['unused_kb']} KB)"
                                                                      for h in unused[:5]) + ".")
        if any(b.get("inline") for b in slow):
            steps.append("„Kod wpisany w HTML strony” to skrypty dodane bezpośrednio w treść (kreator, wtyczki, wklejone kody) — "
                         "przejrzyj sekcje HTML / „Kod niestandardowy” i usuń nieużywane.")
        if env["elementor"]:
            steps.append("Elementor → Ustawienia → Funkcje: „Zoptymalizowane ładowanie zasobów”, „Lazy load obrazów tła”, "
                         "„Buforowanie elementów”; wyłącz nieużywane widżety (Elementor → Menedżer elementów).")
        steps.append("Wyłącz wtyczki, które ładują skrypty na każdej stronie, a są potrzebne na jednej (formularze, slidery) — "
                     "Perfmatters / Asset CleanUp: wyłączanie skryptów per podstrona.")
        task("javascript", "Ogranicz ciężki i nieużywany JavaScript", "wysoki" if sum(b["total"] for b in slow) >= 1500 else "średni",
             "Skrypty", f"Skrypty zajmują procesor łącznie {sum(b['total'] for b in slow)} ms, nieużywane "
             f"{sum(h['unused_kb'] for h in unused)} KB.", steps, snippets,
             gain=f"−{sum(h['unused_kb'] for h in unused)} KB, krótsze blokowanie", files=[b.get("full", "") for b in slow])

    # 5. Czcionki
    font_css = [r for r in rows if "fonts.googleapis.com" in r["url"]]
    font_files = [r for r in rows if r["type"] == "Czcionka"]
    if len(font_css) >= 2 or len(font_files) >= 6 or _opp(psi, "font-display", "font-display-insight"):
        snippets = []
        if env["wp"]:
            snippets.append(php("Szybsze połączenie z Google Fonts (preconnect)", """
// BSK: wcześniejsze połączenie z serwerami czcionek Google
add_action('wp_head', function () {
    echo '<link rel="preconnect" href="https://fonts.googleapis.com">' . "\\n";
    echo '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>' . "\\n";
}, 1);
"""))
        families = sorted({f for r in font_css for f in re.findall(r"family=([^:&|]+)", urllib.parse.unquote(r["url"]))})
        steps = [f"Strona pobiera {len(font_css)} arkuszy Google Fonts i {len(font_files)} plików czcionek"
                 + (f" (rodziny: {', '.join(families[:10])})" if families else "") + ".",
                 "Zostaw 1–2 rodziny w 2–3 grubościach; usuń czcionki dodane przez wtyczki i nieużywane szablony.",
                 "Hostuj czcionki lokalnie (WOFF2) z font-display: swap."]
        if env["elementor"]:
            steps.append("Elementor → Ustawienia → Zaawansowane/Wydajność: „Wczytywanie czcionek Google” = Lokalnie, "
                         "„font-display” = Swap.")
        task("fonts", "Uporządkuj czcionki", "średni", "Szybkość", steps[0], steps[1:], snippets,
             gain=f"−{len(font_css)} zapytań do Google Fonts", files=[r["url"] for r in font_css])

    # 6. Obrazy
    img_opp = [f for f in psi.get("opportunities", []) if f.get("id") in (
        "modern-image-formats", "uses-optimized-images", "uses-responsive-images", "offscreen-images",
        "image-delivery-insight", "efficient-animated-content")]
    big_imgs = sorted((r for r in rows if r["type"] == "Obraz" and r["kb"] >= 150), key=lambda r: -r["kb"])
    if img_opp or big_imgs:
        save = sum(f["save_kb"] for f in img_opp)
        steps = ([f"Google: {f['title']} — " + (f"−{f['save_kb']} KB" if f["save_kb"] else f["display"]) for f in img_opp]
                 + ([f"Największe obrazy: " + ", ".join(f"{_file(r['url'])} ({r['kb']:.0f} KB)" for r in big_imgs[:6])] if big_imgs else [])
                 + ["Konwersja do WebP/AVIF i kompresja: wtyczka ShortPixel / Imagify / Converter for Media (zbiorczo dla biblioteki).",
                    "Wgrywaj zdjęcia w rozmiarze wyświetlania (np. max 1920 px szerokości), produkty ~1000 px.",
                    "Obrazy poniżej pierwszego ekranu z loading=\"lazy\" (WordPress robi to sam), z podanymi width/height."])
        task("images", "Zmniejsz obrazy", "wysoki" if save >= 500 else "średni", "Obrazy",
             f"Obrazy do odchudzenia: ok. {save} KB." if save else f"{len(big_imgs)} obrazów powyżej 150 KB.", steps,
             gain=f"−{save} KB" if save else "", files=[r["url"] for r in big_imgs])

    # 7. Pamięć podręczna i kompresja
    cache = _opp(psi, "uses-long-cache-ttl", "cache-insight")
    ttfb = (net.get("rows") or [{}])[0].get("ms", 0) if rows else 0
    srv = _opp(psi, "server-response-time", "document-latency-insight")
    if cache or srv or ttfb >= 800 or facts.get("compression") == "brak":
        snippets = [{"title": "Pamięć podręczna przeglądarki i kompresja (.htaccess — serwery Apache / LiteSpeed)", "lang": "apache",
                     "where": "Plik .htaccess w głównym katalogu strony (FTP / menedżer plików hostingu), na początku pliku. "
                              "Zrób kopię pliku przed zmianą.", "code": """# BSK: kompresja
<IfModule mod_deflate.c>
  AddOutputFilterByType DEFLATE text/html text/plain text/css text/javascript application/javascript application/json image/svg+xml font/woff2
</IfModule>
# BSK: pamięć podręczna przeglądarki dla plików statycznych
<IfModule mod_expires.c>
  ExpiresActive On
  ExpiresByType image/webp "access plus 1 year"
  ExpiresByType image/avif "access plus 1 year"
  ExpiresByType image/jpeg "access plus 1 year"
  ExpiresByType image/png "access plus 1 year"
  ExpiresByType image/svg+xml "access plus 1 year"
  ExpiresByType font/woff2 "access plus 1 year"
  ExpiresByType text/css "access plus 1 month"
  ExpiresByType application/javascript "access plus 1 month"
  ExpiresByType text/javascript "access plus 1 month"
</IfModule>
<IfModule mod_headers.c>
  <FilesMatch "\\.(webp|avif|jpe?g|png|svg|woff2|css|js)$">
    Header set Cache-Control "public, max-age=2592000"
  </FilesMatch>
</IfModule>
"""}]
        steps = []
        if cache:
            steps.append(f"Google: {cache['title']} — {cache['display'] or str(len(cache['rows'])) + ' plików'}.")
        if srv or ttfb >= 800:
            steps.append(f"Dokument HTML wczytuje się {ttfb} ms — włącz pełne buforowanie stron (LiteSpeed Cache na hostingu "
                         "LiteSpeed, inaczej WP Rocket / W3 Total Cache) i CDN (Cloudflare).")
        if env["wp"]:
            steps.append("Wtyczka cache robi też: minifikację HTML/CSS/JS, łączenie plików, preload stron — po włączeniu "
                         "wyczyść pamięć i sprawdź stronę.")
        task("cache", "Włącz buforowanie i pamięć podręczną", "wysoki" if ttfb >= 1200 else "średni", "Serwer",
             f"Odpowiedź serwera {ttfb} ms; " + ("pliki bez długiej pamięci podręcznej." if cache else "brak buforowania stron."),
             steps, snippets, gain="szybsze kolejne wizyty, TTFB < 600 ms")

    # 8. Za dużo plików CSS/JS
    n_css = sum(1 for r in rows if r["type"] == "CSS")
    n_js = sum(1 for r in rows if r["type"] == "JS")
    if n_css >= 20 or n_js >= 25:
        steps = [f"Strona wczytuje {n_css} plików CSS i {n_js} JS.",
                 "Wtyczka cache: łączenie i minifikacja CSS/JS (LiteSpeed Cache: „CSS Combine”, „JS Combine”; WP Rocket: "
                 "„Optymalizuj pliki CSS/JS”).",
                 "Usuń nieużywane wtyczki i widżety — każda dokłada własne pliki."]
        if env["elementor"]:
            steps.insert(1, "Elementor dzieli style na osobne pliki dla każdego widżetu — włącz „Ulepszone ładowanie CSS” i "
                            "„Zoptymalizowany kod DOM”, a w Elementor → Narzędzia → „Wygeneruj ponownie pliki CSS”.")
        task("requests", "Zmniejsz liczbę plików", "średni", "Szybkość", steps[0], steps[1:], gain=f"−{max(0, n_css - 8)} zapytań CSS")

    # 9. Błędy 404 i przekierowania
    errs = [r for r in rows if r["status"] >= 400]
    if errs:
        task("errors", "Napraw brakujące pliki (błędy 404)", "średni", "Błędy",
             f"{len(errs)} plików zwraca błąd: " + ", ".join(f"{_file(r['url'])} ({r['status']})" for r in errs[:6]) + ".",
             ["Każde takie zapytanie to stracony czas i błąd w konsoli. Usuń odwołanie (stary obrazek, czcionka, skrypt usuniętej "
              "wtyczki) albo wgraj brakujący plik.",
              "Szukaj nazwy pliku w: Elementor (sekcje, tła), Wygląd → Dostosuj → Dodatkowy CSS, ustawienia motywu."],
             files=[r["url"] for r in errs])

    # 10. Przesunięcia układu (CLS)
    cls = metrics.get("cumulative-layout-shift")
    if cls and cls["rating"] != "good":
        unsized = _opp(psi, "unsized-images", "cls-culprits-insight", "layout-shifts")
        task("cls", "Zatrzymaj „skakanie” elementów (CLS)", "średni", "Szybkość", f"Przesunięcia układu {cls['display']} (dobrze < 0,1).",
             ["Obrazy i ramki z wymiarami width/height (albo aspect-ratio w CSS).",
              "Baner cookies, pasek promocji i czat — nie wsuwaj ich nad treścią po wczytaniu; zarezerwuj miejsce.",
              "Czcionki z font-display: swap i podobną czcionką zastępczą."]
             + ([f"Google wskazuje: {', '.join(i['label'] for i in unsized['rows'][:4])}."] if unsized and unsized["rows"] else []))

    # 11. Pozostałe zalecenia Google z dużym zyskiem
    covered = {"render-blocking-resources", "render-blocking-insight", "unused-javascript", "bootup-time", "third-party-summary",
               "third-parties-insight", "modern-image-formats", "uses-optimized-images", "uses-responsive-images",
               "offscreen-images", "image-delivery-insight", "uses-long-cache-ttl", "cache-insight", "server-response-time",
               "document-latency-insight", "font-display", "font-display-insight", "unsized-images", "largest-contentful-paint-element",
               "lcp-discovery-insight", "mainthread-work-breakdown", "network-requests"}
    for f in psi.get("opportunities", []):
        if f["id"] in covered or not (f["save_ms"] >= 150 or f["save_kb"] >= 50 or f["level"] == "error"):
            continue
        gain = " · ".join(x for x in ((f"−{_sec(f['save_ms'])}" if f["save_ms"] >= 100 else ""),
                                       (f"−{f['save_kb']} KB" if f["save_kb"] >= 10 else "")) if x)
        task(f["id"], f["title"], "średni" if f["save_ms"] >= 300 else "niski", "Szybkość (Google)",
             f["title"] + (f" — {f['display']}" if f["display"] else "") + ".", [f["desc"]] if f["desc"] else [],
             gain=gain, files=[i["label"] for i in f["rows"]])

    # 12. Zalogowany administrator w teście (pliki paska admina)
    if any("admin-bar" in r["url"] for r in rows):
        task("admin-bar", "Test wykonano jako zalogowany użytkownik", "niski", "Uwaga",
             "W zapytaniach są pliki paska administratora (admin-bar).",
             ["Zalogowani widzą stronę wolniej niż klienci (pasek admina, brak cache). Sprawdzaj szybkość w oknie incognito."])

    tasks.sort(key=lambda t: PRIO.get(t["prio"], 3))
    php_all = "\n".join(s["code"] for t in tasks for s in t["snippets"] if s["lang"] == "php")
    return {"tasks": tasks, "env": env, "php_all": "<?php\n// BSK Media — poprawki wydajności (wygenerowane przez agenta)\n\n"
            + php_all if php_all else ""}


AI_PROMPT = """Jesteś ekspertem od wydajności stron WWW (Core Web Vitals, WordPress, WooCommerce, Elementor) w agencji BSK Media.
Strona: {url}. Wyniki PageSpeed (telefon): {scores}. Metryki: {metrics}.
Plan zadań przygotowany automatycznie (problem → rozwiązanie):
{tasks}

Napisz po polsku podsumowanie dla zespołu (bez wstępu):
1. „Diagnoza” — 3–4 zdania: co najbardziej spowalnia tę stronę i dlaczego (konkretne pliki, usługi, czasy).
2. „Kolejność prac” — lista 5–8 kroków od największego zysku, każdy z szacowanym czasem pracy (np. 30 min, 2 h)
   i spodziewanym efektem.
3. „Cel” — jakie wyniki są realne po zmianach (wydajność, LCP, blokowanie).
Nie wymyślaj plików ani liczb, których nie ma w danych."""


def ai_prompt(url: str, psi: dict, plan: dict) -> str:
    scores = ", ".join(f"{k} {v}" for k, v in (psi.get("scores") or {}).items())
    metrics = ", ".join(f"{m['name']} {m['display']}" for m in psi.get("metrics", []))
    tasks = "\n".join(f"- [{t['prio']}] {t['title']}: {t['problem']} → " + " ".join(t["steps"][:3])[:500]
                      for t in plan["tasks"][:12])
    return AI_PROMPT.format(url=url, scores=scores, metrics=metrics, tasks=tasks)
