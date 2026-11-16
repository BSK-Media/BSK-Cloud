"""Szybkość strony — pełny wynik PageSpeed Insights (Lighthouse) po polsku.

Zapisuje każdy test (telefon / komputer) w tabeli psi_runs: wyniki kategorii, metryki z progami Google,
dane od prawdziwych użytkowników Chrome (CrUX), element LCP i jego fazy, rekomendacje z oszczędnościami,
diagnostykę, skrypty (czas wykonania, rozmiar, usługi zewnętrzne), kolejność wczytywania plików i klatki
wczytywania strony. Z historii testów powstaje wykres trendu.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .db import Database, now_iso

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
SCHEMA = """
CREATE TABLE IF NOT EXISTS psi_runs (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    strategy TEXT NOT NULL,              -- mobile / desktop
    performance INTEGER,
    lcp_ms INTEGER, tbt_ms INTEGER, fcp_ms INTEGER, si_ms INTEGER, cls REAL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_psi_runs ON psi_runs(site_id, strategy, ts);
"""
# metryka: (audyt Lighthouse, nazwa, próg „dobrze”, próg „słabo”, jednostka)
METRICS = [
    ("first-contentful-paint", "Pierwsza treść (FCP)", 1800, 3000, "ms", "kiedy pojawia się cokolwiek na ekranie"),
    ("largest-contentful-paint", "Główna treść (LCP)", 2500, 4000, "ms", "kiedy widać największy element — zdjęcie, nagłówek"),
    ("total-blocking-time", "Blokowanie (TBT)", 200, 600, "ms", "ile strona „zamarza” przez skrypty — klik nie działa"),
    ("cumulative-layout-shift", "Przesunięcia układu (CLS)", 0.1, 0.25, "", "czy elementy „skaczą” podczas wczytywania"),
    ("speed-index", "Indeks szybkości", 3400, 5800, "ms", "jak szybko wypełnia się widoczna część strony"),
    ("interactive", "Gotowa do użycia (TTI)", 3800, 7300, "ms", "kiedy strona w pełni reaguje"),
]
FIELD = [("LARGEST_CONTENTFUL_PAINT_MS", "Główna treść (LCP)", "ms"),
         ("INTERACTION_TO_NEXT_PAINT", "Reakcja na kliknięcie (INP)", "ms"),
         ("CUMULATIVE_LAYOUT_SHIFT_SCORE", "Przesunięcia układu (CLS)", "cls"),
         ("FIRST_CONTENTFUL_PAINT_MS", "Pierwsza treść (FCP)", "ms"),
         ("EXPERIMENTAL_TIME_TO_FIRST_BYTE", "Odpowiedź serwera (TTFB)", "ms")]
CATEGORY_PL = {"FAST": "dobrze", "AVERAGE": "do poprawy", "SLOW": "słabo", "NONE": "—"}
PHASES_PL = {"TTFB": "Odpowiedź serwera (TTFB)", "Time to first byte": "Odpowiedź serwera (TTFB)",
             "Load Delay": "Opóźnienie startu pobierania", "Resource load delay": "Opóźnienie startu pobierania",
             "Load Time": "Pobieranie pliku", "Resource load duration": "Pobieranie pliku",
             "Render Delay": "Opóźnienie wyświetlenia", "Element render delay": "Opóźnienie wyświetlenia"}
CATS = [("performance", "Wydajność"), ("accessibility", "Dostępność"), ("best-practices", "Dobre praktyki"), ("seo", "SEO")]


def init(db: Database) -> None:
    db.conn().executescript(SCHEMA)


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _md(text: str) -> str:
    """Opis z Lighthouse bez linków Markdown i odnośników „Więcej informacji”."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", text or "")
    text = re.sub(r"\s*(Więcej informacji|Learn more|Dowiedz się więcej)[^.]*\.?\s*$", "", text.strip())
    return text.replace("`", "")[:700]


def short(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    if not p.netloc:
        return url[:80]
    path = p.path if len(p.path) <= 60 else "…" + p.path[-58:]
    return f"{p.netloc}{path}"


def _item_label(it: dict) -> str:
    node = it.get("node") if isinstance(it.get("node"), dict) else {}
    src = it.get("source") if isinstance(it.get("source"), dict) else {}
    for v in (it.get("url"), src.get("url"), node.get("snippet"), node.get("nodeLabel"), it.get("label"),
              it.get("groupLabel"), it.get("entity") if isinstance(it.get("entity"), str) else
              (it.get("entity") or {}).get("text") if isinstance(it.get("entity"), dict) else None):
        if v:
            v = str(v)
            return short(v) if v.startswith("http") else v[:160]
    return ""


def _rows(details: dict, limit: int = 8) -> list[dict]:
    items = details.get("items") if isinstance(details, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("type") == "table" and isinstance(it.get("items"), list):  # zagnieżdżone tabele (nowsze wersje)
            out += _rows(it, limit)
            continue
        label = _item_label(it)
        if not label:
            continue
        row = {"label": label}
        if it.get("wastedMs") or it.get("duration") or it.get("total"):
            row["ms"] = round(_num(it.get("wastedMs") or it.get("duration") or it.get("total")))
        b = it.get("wastedBytes") or it.get("transferSize") or it.get("totalBytes")
        if b:
            row["kb"] = round(_num(b) / 1024)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _findings(lh: dict, category: str) -> list[dict]:
    """Nieudane audyty kategorii: tytuł, opis, wynik, oszczędność czasu/rozmiaru, najważniejsze elementy."""
    audits = lh.get("audits", {})
    refs = ((lh.get("categories") or {}).get(category) or {}).get("auditRefs") or []
    out = []
    for ref in refs:
        a = audits.get(ref.get("id")) or {}
        if ref.get("group") == "metrics" or a.get("scoreDisplayMode") in ("notApplicable", "manual", "error"):
            continue
        score = a.get("score")
        if score is None and a.get("scoreDisplayMode") != "informative":
            continue
        if score is not None and score >= 0.9:
            continue
        details = a.get("details") or {}
        save_ms = _num(details.get("overallSavingsMs")) or sum(_num(v) for k, v in (a.get("metricSavings") or {}).items()
                                                               if k in ("LCP", "FCP", "TBT"))
        save_kb = _num(details.get("overallSavingsBytes")) / 1024
        if a.get("scoreDisplayMode") == "informative" and not (a.get("displayValue") or save_ms):
            continue
        out.append({"id": ref.get("id"), "title": str(a.get("title", ""))[:200], "desc": _md(a.get("description", "")),
                    "display": str(a.get("displayValue", ""))[:120], "score": score,
                    "level": "error" if score is not None and score < 0.5 else "warn",
                    "save_ms": round(save_ms), "save_kb": round(save_kb), "rows": _rows(details)})
    out.sort(key=lambda f: (-f["save_ms"], -f["save_kb"], f["score"] if f["score"] is not None else 1))
    return out


def _lcp_element(audits: dict) -> dict:
    a = (audits.get("largest-contentful-paint-element") or audits.get("lcp-discovery-insight") or {}).get("details") or {}
    out: dict = {"snippet": "", "selector": "", "phases": []}
    tables = [a] if a.get("type") == "table" else [t for t in a.get("items", []) if isinstance(t, dict)]
    for t in tables:
        for it in t.get("items", []) if isinstance(t.get("items"), list) else []:
            if not isinstance(it, dict):
                continue
            if isinstance(it.get("node"), dict) and not out["snippet"]:
                out["snippet"] = str(it["node"].get("snippet", ""))[:300]
                out["selector"] = str(it["node"].get("nodeLabel") or it["node"].get("selector", ""))[:200]
            if it.get("phase") or it.get("subpart"):
                name = str(it.get("phase") or it.get("label") or it.get("subpart"))
                out["phases"].append({"phase": PHASES_PL.get(name, name),
                                      "ms": round(_num(it.get("timing") or it.get("duration")))})
    return out


def _waterfall(audits: dict) -> tuple[list[dict], float]:
    items = ((audits.get("network-requests") or {}).get("details") or {}).get("items") or []
    rows = []
    for it in items:
        if not isinstance(it, dict) or not it.get("url"):
            continue
        start = it.get("networkRequestTime", it.get("startTime"))
        end = it.get("networkEndTime", it.get("endTime"))
        if start is None or end is None:
            continue
        rows.append({"url": short(str(it["url"])), "type": str(it.get("resourceType", "")), "start": _num(start),
                     "end": _num(end), "kb": round(_num(it.get("transferSize")) / 1024)})
    if not rows:
        return [], 0
    t0 = min(r["start"] for r in rows)
    for r in rows:
        r["start"], r["end"] = round(r["start"] - t0), round(r["end"] - t0)
    rows.sort(key=lambda r: r["start"])
    return rows[:60], max(r["end"] for r in rows)


TYPE_PL = {"Document": "Dokument", "Script": "JS", "Stylesheet": "CSS", "Image": "Obraz", "Font": "Czcionka",
           "Fetch": "Fetch/XHR", "XHR": "Fetch/XHR", "Media": "Media", "Manifest": "Manifest", "Other": "Inne",
           "Ping": "Inne", "Preflight": "Inne", "TextTrack": "Inne", "WebSocket": "Socket"}


def network(audits: dict, page_url: str = "") -> dict:
    """Pełna lista zapytań jak w F12 → Network (z testu Chrome w PageSpeed) i podsumowanie."""
    items = ((audits.get("network-requests") or {}).get("details") or {}).get("items") or []
    host = urllib.parse.urlsplit(page_url).netloc.removeprefix("www.")
    blocking = {str(i.get("url")) for i in ((audits.get("render-blocking-resources") or {}).get("details") or {}).get("items", [])
                if isinstance(i, dict)}
    rows = []
    for it in items:
        if not isinstance(it, dict) or not it.get("url"):
            continue
        start = it.get("networkRequestTime", it.get("rendererStartTime", it.get("startTime")))
        end = it.get("networkEndTime", it.get("endTime"))
        if start is None or end is None:
            continue
        url = str(it["url"])
        p = urllib.parse.urlsplit(url)
        name = (p.path.rsplit("/", 1)[-1] or p.path or p.netloc) + (("?" + p.query) if p.query else "")
        rtype = str(it.get("resourceType", "Other"))
        rows.append({
            "url": url[:600], "name": name[:160] or url[:160], "domain": p.netloc,
            "third": bool(host) and not p.netloc.removeprefix("www.").endswith(host),
            "type": TYPE_PL.get(rtype, rtype), "rtype": rtype, "mime": str(it.get("mimeType", ""))[:60],
            "status": int(_num(it.get("statusCode"))), "priority": str(it.get("priority", "")),
            "protocol": str(it.get("protocol", "")), "start": _num(start), "end": _num(end),
            "kb": round(_num(it.get("transferSize")) / 1024, 1), "res_kb": round(_num(it.get("resourceSize")) / 1024, 1),
            "entity": str(it.get("entity") or "")[:80], "blocking": url in blocking})
    if not rows:
        return {"rows": [], "summary": {}, "domains": []}
    t0 = min(r["start"] for r in rows)
    for r in rows:
        r["start"], r["end"] = round(r["start"] - t0), round(r["end"] - t0)
        r["ms"] = r["end"] - r["start"]
    rows.sort(key=lambda r: r["start"])
    m = (((audits.get("metrics") or {}).get("details") or {}).get("items") or [{}])[0]
    summary = {"requests": len(rows), "kb": round(sum(r["kb"] for r in rows)), "res_kb": round(sum(r["res_kb"] for r in rows)),
               "finish": max(r["end"] for r in rows),
               "dcl": round(_num(m.get("observedDomContentLoaded"))) if isinstance(m, dict) else 0,
               "load": round(_num(m.get("observedLoad"))) if isinstance(m, dict) else 0,
               "third_kb": round(sum(r["kb"] for r in rows if r["third"])), "third_n": sum(1 for r in rows if r["third"]),
               "errors": sum(1 for r in rows if r["status"] >= 400)}
    doms: dict[str, list] = {}
    for r in rows:
        d = doms.setdefault(r["domain"], [0, 0.0, 0, r["third"]])
        d[0] += 1
        d[1] += r["kb"]
        d[2] = max(d[2], r["end"])
    domains = sorted(({"domain": k, "n": v[0], "kb": round(v[1]), "end": v[2], "third": v[3]} for k, v in doms.items()),
                     key=lambda x: -x["kb"])[:20]
    long_tasks = sorted(({"url": short(str(i.get("url", ""))) or "(główny wątek)", "ms": round(_num(i.get("duration"))),
                          "start": round(_num(i.get("startTime")))}
                         for i in ((audits.get("long-tasks") or {}).get("details") or {}).get("items", []) if isinstance(i, dict)),
                        key=lambda x: -x["ms"])[:15]
    return {"rows": rows[:500], "summary": summary, "domains": domains, "long_tasks": long_tasks}


def parse(data: dict, page_url: str = "") -> dict:
    """Pełny wynik PageSpeed (JSON z API) -> słownik do zapisu i panelu."""
    from .gsc import script_details
    lh = data.get("lighthouseResult", {})
    audits = lh.get("audits", {})
    cats = lh.get("categories", {})
    scores = {k: (round(cats[k]["score"] * 100) if isinstance((cats.get(k) or {}).get("score"), (int, float)) else None)
              for k, _ in CATS}
    metrics = []
    for aid, name, good, poor, unit, hint in METRICS:
        a = audits.get(aid) or {}
        if "numericValue" not in a:
            continue
        v = _num(a.get("numericValue"))
        metrics.append({"id": aid, "name": name, "value": v, "display": str(a.get("displayValue", "")),
                        "rating": "good" if v <= good else ("poor" if v > poor else "warn"),
                        "good": good, "poor": poor, "unit": unit, "hint": hint})
    field = []
    for src_key, label in (("loadingExperience", "ta strona"), ("originLoadingExperience", "cała domena")):
        le = data.get(src_key) or {}
        m = le.get("metrics") or {}
        if not m:
            continue
        for key, name, unit in FIELD:
            if key in m:
                p = _num(m[key].get("percentile"))
                field.append({"name": name, "value": p / 100 if unit == "cls" else p, "unit": unit,
                              "rating": CATEGORY_PL.get(m[key].get("category", "NONE"), "—"),
                              "cat": m[key].get("category", "NONE")})
        field_src = label
        break
    else:
        field_src = ""
    thumbs = [{"t": round(_num(i.get("timing"))), "img": str(i.get("data", ""))}
              for i in ((audits.get("screenshot-thumbnails") or {}).get("details") or {}).get("items", [])
              if isinstance(i, dict) and str(i.get("data", "")).startswith("data:image/")][:10]
    final = str(((audits.get("final-screenshot") or {}).get("details") or {}).get("data", ""))
    wf, wf_end = _waterfall(audits)
    scripts = script_details(audits)
    page_short = short(page_url or data.get("id", "")) if (page_url or data.get("id")) else ""
    for b in scripts["bootup"]:
        b["inline"] = bool(page_short) and b["url"].rstrip("/") == page_short.rstrip("/")
        if b["inline"]:
            b["url"] = "Kod wpisany w HTML strony (skrypty inline, wtyczki, kreator)"
    weight = round(_num((audits.get("total-byte-weight") or {}).get("numericValue")) / 1024)
    reqs = len(((audits.get("network-requests") or {}).get("details") or {}).get("items") or [])
    by_type = sorted(([str(it.get("label") or it["resourceType"]), int(_num(it.get("requestCount"))),
                       round(_num(it.get("transferSize")) / 1024)]
                      for it in ((audits.get("resource-summary") or {}).get("details") or {}).get("items", [])
                      if isinstance(it, dict) and it.get("resourceType") not in ("total", None) and _num(it.get("requestCount"))),
                     key=lambda x: -x[2])
    return {
        "scores": scores, "metrics": metrics, "field": field, "field_src": field_src,
        "lcp_element": _lcp_element(audits),
        "opportunities": _findings(lh, "performance"), "seo_findings": _findings(lh, "seo"),
        "a11y_findings": _findings(lh, "accessibility"), "bp_findings": _findings(lh, "best-practices"),
        "scripts": scripts, "waterfall": wf, "waterfall_end": wf_end, "filmstrip": thumbs,
        "network": network(audits, page_url or str(data.get("id", ""))),
        "final_screenshot": final if final.startswith("data:image/") else "",
        "weight_kb": weight, "requests": reqs, "by_type": by_type,
        "lh_version": str(lh.get("lighthouseVersion", "")), "fetched": str(lh.get("fetchTime", "")),
    }


def fetch(url: str, key: str, strategy: str = "mobile", base: str = PSI_URL) -> dict:
    q = urllib.parse.urlencode([("url", url), ("strategy", strategy), ("key", key), ("locale", "pl"),
                                ("category", "performance"), ("category", "seo"), ("category", "accessibility"),
                                ("category", "best-practices")])
    with urllib.request.urlopen(f"{base}?{q}", timeout=240) as r:  # noqa: S310 — adres z konfiguracji
        return json.loads(r.read())


def _metric(full: dict, aid: str) -> float | None:
    return next((m["value"] for m in full["metrics"] if m["id"] == aid), None)


def store(db: Database, site_id: int, strategy: str, full: dict) -> None:
    db.run("INSERT INTO psi_runs (site_id, ts, strategy, performance, lcp_ms, tbt_ms, fcp_ms, si_ms, cls, data)"
           " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", site_id, now_iso(), strategy, full["scores"].get("performance"),
           _metric(full, "largest-contentful-paint"), _metric(full, "total-blocking-time"),
           _metric(full, "first-contentful-paint"), _metric(full, "speed-index"),
           _metric(full, "cumulative-layout-shift"), json.dumps(full, ensure_ascii=False))
    # starsze testy: zostają liczby do wykresu, bez obrazów i listy zapytań (miejsce w bazie)
    old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    for r in db.all("SELECT id, data FROM psi_runs WHERE site_id = ? AND ts < ? AND data NOT LIKE '%\"trimmed\": true%'",
                    site_id, old):
        d = json.loads(r["data"])
        d["filmstrip"], d["final_screenshot"], d["trimmed"] = [], "", True
        if isinstance(d.get("network"), dict):
            d["network"]["rows"] = []
        db.run("UPDATE psi_runs SET data = ? WHERE id = ?", json.dumps(d, ensure_ascii=False), r["id"])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    db.run("DELETE FROM psi_runs WHERE site_id = ? AND ts < ?", site_id, cutoff)


def compact(full: dict) -> dict:
    """Skrót do integracji / audytu (zgodny ze starszym formatem) i lista uwag."""
    m = {x["id"]: x for x in full["metrics"]}
    lcp, tbt = m.get("largest-contentful-paint", {}), m.get("total-blocking-time", {})
    out = {"performance": full["scores"].get("performance"), "seo": full["scores"].get("seo"),
           "accessibility": full["scores"].get("accessibility"), "best_practices": full["scores"].get("best-practices"),
           "lcp": lcp.get("display", ""), "cls": m.get("cumulative-layout-shift", {}).get("display", ""),
           "fcp": m.get("first-contentful-paint", {}).get("display", ""), "tbt": tbt.get("display", ""),
           "si": m.get("speed-index", {}).get("display", ""), "tbt_ms": round(tbt.get("value", 0)),
           "weight_kb": full["weight_kb"], "scripts": full["scripts"], "issues": issues(full)}
    return out


def issues(full: dict, label: str = "telefon") -> list[dict]:
    """Konkretne uwagi: co dokładnie spowalnia, ile to trwa i ile można zyskać."""
    out = []
    perf = full["scores"].get("performance")
    m = {x["id"]: x for x in full["metrics"]}
    bad = [x for x in full["metrics"] if x["rating"] != "good"]
    if perf is not None and perf < 90:
        details = ", ".join(f"{x['name'].split(' (')[0]} {x['display']}" for x in bad[:4])
        out.append({"level": "error" if perf < 50 else "warn",
                    "text": f"PageSpeed ({label}): wydajność {perf}/100" + (f" — {details}." if details else ".")})
    lcp = m.get("largest-contentful-paint")
    el = full["lcp_element"]
    if lcp and lcp["rating"] != "good":
        phases = ", ".join(f"{p['phase']} {p['ms']} ms" for p in el["phases"] if p["ms"])
        out.append({"level": "error" if lcp["rating"] == "poor" else "warn",
                    "text": f"Główna treść pojawia się po {lcp['display']} (dobrze < 2,5 s)"
                            + (f"; to element: {el['selector'] or el['snippet'][:90]}" if el["snippet"] or el["selector"] else "")
                            + (f" ({phases})" if phases else "") + "."})
    tbt = m.get("total-blocking-time")
    sc = full["scripts"]
    if tbt and tbt["rating"] != "good":
        culprits = [f"{t['entity']} {t['blocking']} ms" for t in sc["third_party"] if t["blocking"]][:3] + \
                   [f"{b['url'].split('/')[-1] or b['url']} {b['total']} ms" for b in sc["bootup"][:3]]
        out.append({"level": "error" if tbt["rating"] == "poor" else "warn",
                    "text": f"Skrypty blokują stronę przez {tbt['display']} (dobrze < 200 ms)"
                            + (f" — najwięcej: {', '.join(culprits[:4])}." if culprits else ".")})
    slow = [b for b in sc["bootup"] if b["total"] >= 400]
    if slow:
        out.append({"level": "warn", "text": "Najdłużej wykonujące się skrypty: " + "; ".join(
            f"{b['url']} — {b['total']} ms (wykonanie {b['scripting']} ms, wczytanie {b['parse']} ms)" for b in slow[:4]) + "."})
    rb = sc["render_blocking"]
    if rb and sum(b["ms"] for b in rb) >= 300:
        out.append({"level": "warn", "text": f"{len(rb)} plików blokuje wyświetlenie strony: " + ", ".join(
            f"{b['url'].split('/')[-1] or b['url']} ({b['ms']} ms)" for b in rb[:5]) + "."})
    waste = sum(h["unused_kb"] for h in sc["heavy"])
    if waste >= 150:
        top = [h for h in sc["heavy"] if h["unused_kb"]][:3]
        out.append({"level": "warn", "text": f"Ok. {waste} KB JavaScriptu pobieranego, ale nieużywanego: " + ", ".join(
            f"{h['url'].split('/')[-1]} ({h['unused_kb']} KB)" for h in top) + "."})
    for f in full["opportunities"][:4]:
        if f["save_ms"] >= 300 or f["save_kb"] >= 200:
            gain = " i ".join(x for x in ((f"{f['save_ms'] / 1000:.1f} s".replace(".", ",") if f["save_ms"] >= 100 else ""),
                                           (f"{f['save_kb']} KB" if f["save_kb"] >= 20 else "")) if x)
            what = ", ".join(i["label"].split("/")[-1][:50] for i in f["rows"][:3])
            out.append({"level": "warn", "text": f"{f['title']}" + (f" — do zyskania ok. {gain}" if gain else "")
                        + (f" ({what})" if what else "") + "."})
    return out
