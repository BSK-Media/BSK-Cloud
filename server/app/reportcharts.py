"""Wykresy raportu strony (SVG po stronie serwera, zgodne z CSP — bez skryptów i stylów w atrybutach).

- time_series: przebieg dzienny, kilka serii, seria porównawcza przerywaną linią, podpowiedź przy każdym punkcie,
- donut: udział (np. urządzenia),
- waterfall: kolejność i czas wczytywania plików strony (jak w narzędziach przeglądarki).
"""
from __future__ import annotations

import math
from datetime import date

from markupsafe import Markup, escape

BLUE, ORANGE, GREEN, PURPLE, GREY = "#2a78d6", "#eb6834", "#0ca30c", "#7c4dce", "#9aa3b2"
DASH_LEGEND = ' stroke-dasharray="4 3"'
PALETTE = [BLUE, ORANGE, GREEN, PURPLE, "#d03b3b", "#00a3a3", "#c08b00", GREY]
TYPE_COLORS = {"Document": "#2a78d6", "Script": "#e0a100", "Stylesheet": "#7c4dce", "Image": "#0ca30c",
               "Font": "#d03b3b", "Fetch": "#00a3a3", "XHR": "#00a3a3", "Media": "#eb6834"}


def nice_step(raw: float, lines: int = 4) -> float:
    if raw <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(raw / lines))
    for m in (1, 2, 2.5, 5, 10):
        if raw / (m * mag) <= lines:
            return m * mag
    return 10 * mag


def fmt(n: float, unit: str = "") -> str:
    if unit == "czas":
        n = int(round(n or 0))
        return f"{n // 60}:{n % 60:02d}"
    if unit == "%":
        return f"{n:.1f}%".replace(".", ",")
    if unit == "s":
        return f"{n:.1f} s".replace(".", ",")
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f} mln".replace(".", ",")
    if abs(n) >= 10_000:
        return f"{n / 1000:.1f} tys.".replace(".", ",")
    if n != int(n):
        return f"{n:.1f}".replace(".", ",")
    return f"{int(n):,}".replace(",", " ")


def time_series(days: list[date], series: list[tuple], title: str, unit: str = "", height: int = 240) -> Markup:
    """series: [(nazwa, wartości, kolor, przerywana)] — wartości wyrównane do `days` (seria porównawcza ma
    tyle samo punktów: dzień 1 okresu porównawczego pod dniem 1 bieżącego)."""
    W, H, L, R, T, B = 760, height, 52, 12, 14, 28
    pw, ph = W - L - R, H - T - B
    n = len(days)
    vals = [v for s in series for v in s[1] if v is not None]
    if n < 2 or not vals:
        return Markup('<div class="chart-wait muted small">Brak danych w wybranym okresie.</div>')
    step = nice_step(max(vals))
    top = step * max(1, math.ceil(max(vals) / step))
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart rchart" role="img" aria-label="{escape(title)}">']
    for i in range(int(round(top / step)) + 1):
        v = step * i
        y = T + ph - ph * v / top
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{L - 8}" y="{y + 4:.1f}" text-anchor="end" class="tick">{escape(fmt(v, unit))}</text>')

    def xy(i, v):
        return L + pw * i / (n - 1), T + ph - ph * max(0, v or 0) / top
    for name, values, color, dashed in reversed(series):
        pts = [xy(i, v) for i, v in enumerate(values[:n]) if v is not None]
        if len(pts) < 2:
            continue
        path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        if not dashed:
            out.append(f'<path d="{path} L{pts[-1][0]:.1f},{T + ph} L{pts[0][0]:.1f},{T + ph} Z" fill="{color}" fill-opacity=".08"/>')
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        out.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{1.6 if dashed else 2.2}"'
                   f'{dash} stroke-linejoin="round" stroke-linecap="round"/>')
    # punkty z podpowiedzią: data + wszystkie serie
    for i, d in enumerate(days):
        x = L + pw * i / (n - 1)
        tip = d.strftime("%d.%m.%Y") + "".join(
            f"\n{name}: {fmt(values[i], unit) if i < len(values) and values[i] is not None else '—'}"
            for name, values, _, _ in series)
        out.append(f'<rect x="{x - pw / (n - 1) / 2:.1f}" y="{T}" width="{pw / (n - 1):.1f}" height="{ph}" class="hit">'
                   f'<title>{escape(tip)}</title></rect>')
        if n <= 45:
            name, values, color, _ = series[0]
            if i < len(values) and values[i] is not None:
                cx, cy = xy(i, values[i])
                out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="2.6" fill="{color}"/>')
    ticks = min(6, n)
    for k in range(ticks):
        i = round(k * (n - 1) / (ticks - 1))
        x = L + pw * i / (n - 1)
        anchor = "start" if k == 0 else ("end" if k == ticks - 1 else "middle")
        out.append(f'<text x="{x:.1f}" y="{H - 8}" text-anchor="{anchor}" class="tick">{days[i].strftime("%d.%m")}</text>')
    out.append("</svg>")
    legend = "".join(f'<span class="lg"><svg width="18" height="8"><line x1="0" x2="18" y1="4" y2="4" stroke="{c}" '
                     f'stroke-width="2.5"{DASH_LEGEND if dsh else ""}/></svg>{escape(nm)}</span>'
                     for nm, _, c, dsh in series)
    return Markup(f'<div class="rchart-wrap">{"".join(out)}<div class="legend">{legend}</div></div>')


def donut(items: list[tuple[str, float]], title: str, size: int = 150) -> Markup:
    total = sum(v for _, v in items) or 0
    if not total:
        return Markup('<div class="chart-wait muted small">Brak danych.</div>')
    r, cx, sw = size / 2 - 12, size / 2, 22
    circ = 2 * math.pi * r
    out = [f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" class="donut" role="img" aria-label="{escape(title)}">']
    off = 0.0
    for i, (label, v) in enumerate(items):
        frac = v / total
        out.append(f'<circle cx="{cx}" cy="{cx}" r="{r:.1f}" fill="none" stroke="{PALETTE[i % len(PALETTE)]}" '
                   f'stroke-width="{sw}" stroke-dasharray="{circ * frac:.2f} {circ:.2f}" stroke-dashoffset="{-off:.2f}" '
                   f'transform="rotate(-90 {cx} {cx})"><title>{escape(label)}: {fmt(v)} ({frac * 100:.0f}%)</title></circle>')
        off += circ * frac
    out.append("</svg>")
    legend = "".join(f'<div class="dl"><span class="sw sw{i % len(PALETTE)}"></span>{escape(label)}'
                     f'<b>{v / total * 100:.0f}%</b><span class="muted">{fmt(v)}</span></div>'
                     for i, (label, v) in enumerate(items))
    return Markup(f'<div class="donut-wrap">{"".join(out)}<div class="donut-legend">{legend}</div></div>')


def waterfall(reqs: list[dict], total_ms: float | None = None) -> Markup:
    """reqs: [{url, type, start, end, kb}] w ms od początku wczytywania."""
    rows = [r for r in reqs if r.get("end", 0) >= r.get("start", 0)][:45]
    if not rows:
        return Markup('<div class="chart-wait muted small">Brak danych o plikach.</div>')
    end = max(total_ms or 0, max(r["end"] for r in rows)) or 1
    W, L, RH, T = 760, 300, 16, 22
    pw = W - L - 70
    H = T + RH * len(rows) + 6
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart waterfall" role="img" aria-label="Kolejność wczytywania plików">']
    step = nice_step(end, 6)
    t = 0.0
    while t <= end:
        x = L + pw * t / end
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{T - 4}" y2="{H}" class="grid"/>'
                   f'<text x="{x:.1f}" y="{T - 8}" text-anchor="middle" class="tick">{fmt(t / 1000, "s")}</text>')
        t += step
    for i, r in enumerate(rows):
        y = T + i * RH
        color = TYPE_COLORS.get(r.get("type", ""), "#9aa3b2")
        x1 = L + pw * r["start"] / end
        w = max(2.0, pw * (r["end"] - r["start"]) / end)
        name = r["url"] if len(r["url"]) <= 46 else "…" + r["url"][-45:]
        tip = (f"{r['url']}\n{r.get('type', '')} · {r.get('kb', 0)} KB\nstart {r['start']:.0f} ms, "
               f"koniec {r['end']:.0f} ms ({r['end'] - r['start']:.0f} ms)")
        out.append(f'<g><title>{escape(tip)}</title>'
                   f'<text x="{L - 8}" y="{y + 11}" text-anchor="end" class="wf-name">{escape(name)}</text>'
                   f'<rect x="{x1:.1f}" y="{y + 3}" width="{w:.1f}" height="{RH - 6}" rx="2" fill="{color}"/>'
                   f'<text x="{min(x1 + w + 4, W - 66):.1f}" y="{y + 11}" class="wf-ms">{r["end"] - r["start"]:.0f} ms</text></g>')
    out.append("</svg>")
    used = sorted({r.get("type", "") for r in rows})
    legend = "".join(f'<span class="lg"><svg width="12" height="10"><rect width="12" height="10" rx="2" '
                     f'fill="{TYPE_COLORS.get(t_, "#9aa3b2")}"/></svg>{escape(t_ or "inne")}</span>' for t_ in used)
    return Markup(f'<div class="rchart-wrap">{"".join(out)}<div class="legend">{legend}</div></div>')


def score_ring(score: int | None, label: str, size: int = 92) -> Markup:
    """Wynik 0–100 jak w PageSpeed: czerwony < 50, pomarańczowy < 90, zielony >= 90."""
    color = "#9aa3b2" if score is None else ("#d03b3b" if score < 50 else ("#e08600" if score < 90 else "#0ca30c"))
    r, c = size / 2 - 7, size / 2
    circ = 2 * math.pi * r
    frac = (score or 0) / 100
    return Markup(
        f'<div class="score-ring"><svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" '
        f'aria-label="{escape(label)}: {score if score is not None else "brak"}">'
        f'<circle cx="{c}" cy="{c}" r="{r:.1f}" fill="{color}" fill-opacity=".08" stroke="{color}" stroke-opacity=".18" stroke-width="6"/>'
        f'<circle cx="{c}" cy="{c}" r="{r:.1f}" fill="none" stroke="{color}" stroke-width="6" stroke-linecap="round" '
        f'stroke-dasharray="{circ * frac:.2f} {circ:.2f}" transform="rotate(-90 {c} {c})"/>'
        f'<text x="{c}" y="{c + 8}" text-anchor="middle" class="ring-num" fill="{color}">{score if score is not None else "—"}</text>'
        f'</svg><div class="ring-label">{escape(label)}</div></div>')


def bar(value: float, top: float, color: str = BLUE, width: int = 120) -> Markup:
    w = 0 if not top else max(2, width * value / top)
    return Markup(f'<svg width="{width}" height="8" class="mini-bar" aria-hidden="true"><rect width="{width}" height="8" rx="4" '
                  f'fill="#eef1f6"/><rect width="{w:.1f}" height="8" rx="4" fill="{color}"/></svg>')


def wf_bar(start: float, end: float, total: float, rtype: str = "", width: int = 220) -> Markup:
    """Pasek osi czasu w wierszu tabeli Network."""
    total = total or 1
    x = width * start / total
    w = max(2.0, width * (end - start) / total)
    color = TYPE_COLORS.get(rtype, "#9aa3b2")
    return Markup(f'<svg width="{width}" height="10" class="wf-row" aria-hidden="true"><rect width="{width}" height="10" fill="#f3f5f8" rx="2"/>'
                  f'<rect x="{x:.1f}" width="{w:.1f}" height="10" rx="2" fill="{color}"/></svg>')
