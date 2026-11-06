"""Wykresy SVG generowane po stronie serwera (bez zewnętrznych bibliotek — zgodne z CSP).

Zasady (paleta zwalidowana pod daltonizm): seria 1 = niebieski, seria 2 = pomarańczowy; cienkie
słupki z zaokrąglonym końcem, 2 px odstępu między segmentami, siatka 1 px, legenda przy >= 2 seriach,
podpowiedź po najechaniu (<title>) i tabela z danymi dla czytników ekranu.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from markupsafe import Markup, escape

from .storage import human_size

SERIES = ["#2a78d6", "#eb6834"]
TRACK = "#e8f0fb"
GRID = "#ebedf0"
MUTED = "#6b7280"
WARNING = "#fab219"
CRITICAL = "#d03b3b"
GOOD = "#0ca30c"


def _fmt(n: float) -> str:
    if n >= 10_000:
        return f"{n / 1000:.0f}K"
    return f"{int(n):,}".replace(",", " ")


def _column(x: float, y_top: float, w: float, h: float, round_top: bool) -> str:
    """Słupek kwadratowy przy osi, z zaokrąglonym (4 px) końcem danych."""
    if h <= 0:
        return ""
    r = min(4.0, w / 2, h) if round_top else 0
    b = y_top + h
    return (f"M{x:.1f},{b:.1f}V{y_top + r:.1f}Q{x:.1f},{y_top:.1f} {x + r:.1f},{y_top:.1f}"
            f"H{x + w - r:.1f}Q{x + w:.1f},{y_top:.1f} {x + w:.1f},{y_top + r:.1f}V{b:.1f}Z")


def stacked_columns(days: list[dict], series: list[tuple[str, str]], title: str, unit: str = "plików") -> Markup:
    """days: [{'date': date, key1: n, key2: n}], series: [(key, etykieta)]."""
    W, H, L, R, T, B = 720, 220, 34, 8, 12, 26
    pw, ph = W - L - R, H - T - B
    n = max(1, len(days))
    band = pw / n
    bw = min(24.0, band * 0.62)
    totals = [sum(d.get(k, 0) for k, _ in series) for d in days]
    raw = max(totals or [0])
    # liczby całkowite: krok 1/2/5/10… tak, żeby wyszło najwyżej 5 linii siatki
    step = 1
    for m in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000):
        step = m
        if raw / m <= 4:
            break
    ticks = max(1, math.ceil(raw / step))
    top = step * ticks
    parts = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{escape(title)}">']
    for i in range(ticks + 1):
        v = step * i
        y = T + ph - ph * i / ticks
        parts.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{L - 6}" y="{y + 4:.1f}" text-anchor="end" class="tick">{_fmt(v)}</text>')
    for i, d in enumerate(days):
        x = L + band * i + (band - bw) / 2
        base = T + ph
        visible = [(k, d.get(k, 0)) for k, _ in series if d.get(k, 0) > 0]
        for j, (k, v) in enumerate(visible):
            h = ph * v / top
            gap = 2 if j < len(visible) - 1 else 0
            color = SERIES[[s[0] for s in series].index(k)]
            parts.append(f'<path d="{_column(x, base - h, bw, h - gap, j == len(visible) - 1)}" fill="{color}"/>')
            base -= h
        tip = f"{d['date'].strftime('%d.%m')}: " + ", ".join(f"{lbl.lower()} {d.get(k, 0)}" for k, lbl in series)
        parts.append(f'<rect x="{L + band * i:.1f}" y="{T}" width="{band:.1f}" height="{ph}" fill="transparent">'
                     f'<title>{escape(tip)}</title></rect>')
        if (n - 1 - i) % 7 == 0:  # co tydzień, licząc od dzisiaj — bez nachodzenia etykiet
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{H - 8}" text-anchor="middle" class="tick">'
                         f'{d["date"].strftime("%d.%m")}</text>')
    parts.append("</svg>")
    legend = "".join(
        f'<span class="legend-item">{swatch(SERIES[i])}{escape(lbl)}</span>' for i, (_, lbl) in enumerate(series)
    ) if len(series) > 1 else ""
    rows = "".join(
        f"<tr><td>{d['date'].strftime('%d.%m.%Y')}</td>" + "".join(f"<td>{d.get(k, 0)}</td>" for k, _ in series) + "</tr>"
        for d in days if any(d.get(k, 0) for k, _ in series)
    ) or f'<tr><td colspan="{len(series) + 1}">Brak danych w tym okresie.</td></tr>'
    table = (f'<details class="chart-data"><summary>Pokaż dane</summary><table class="list compact"><thead><tr><th>Dzień</th>'
             + "".join(f"<th>{escape(lbl)}</th>" for _, lbl in series) + f"</tr></thead><tbody>{rows}</tbody></table></details>")
    return Markup(f'<div class="legend">{legend}</div>' + "".join(parts) + table)


def swatch(color: str) -> str:
    return f'<svg width="10" height="10" aria-hidden="true"><rect width="10" height="10" rx="2" fill="{color}"/></svg>'


def _bar_svg(pct: float, color: str, height: int = 8, track: str = TRACK) -> str:
    """Poziomy pasek w SVG (szerokość w %, bez stylów inline — CSP)."""
    pct = max(0.0, min(100.0, pct))
    r = height / 2
    fill = (f'<rect width="{pct:.2f}%" height="{height}" rx="{r}" fill="{color}"/>' if pct > 0 else "")
    return (f'<svg class="bar" width="100%" height="{height}" preserveAspectRatio="none" aria-hidden="true">'
            f'<rect width="100%" height="{height}" rx="{r}" fill="{track}"/>{fill}</svg>')


def level_color(ratio: float) -> str:
    return CRITICAL if ratio >= 0.9 else WARNING if ratio >= 0.8 else SERIES[0]


def meter(used: int, total: int, label: str = "") -> Markup:
    """Pasek zajętości: niebieski -> żółty (>= 80%) -> czerwony (>= 90%), ze słownym stanem."""
    ratio = used / total if total else 0
    pct = min(100.0, ratio * 100)
    state = ""
    if ratio >= 0.9:
        state = '<span class="state critical">● prawie pełny</span>'
    elif ratio >= 0.8:
        state = '<span class="state warning">● kończy się miejsce</span>'
    tip = f"{label + ': ' if label else ''}{human_size(used)} z {human_size(total)} ({pct:.0f}%)"
    return Markup(
        f'<div class="meter" title="{escape(tip)}" role="meter" aria-valuemin="0" aria-valuemax="100" '
        f'aria-valuenow="{pct:.0f}" aria-label="{escape(tip)}">{_bar_svg(pct, level_color(ratio))}</div>{state}'
    )


def ring(used: int, total: int, size: int = 132) -> Markup:
    """Pierścień limitu miejsca z procentem w środku."""
    ratio = min(1.0, used / total) if total else 0
    r = size / 2 - 9
    c = 2 * math.pi * r
    color = level_color(ratio)
    tip = f"{human_size(used)} z {human_size(total)}"
    return Markup(
        f'<svg viewBox="0 0 {size} {size}" class="ring" width="{size}" height="{size}" role="img" aria-label="{escape(tip)}">'
        f'<title>{escape(tip)}</title>'
        f'<circle cx="{size / 2}" cy="{size / 2}" r="{r:.1f}" fill="none" stroke="{TRACK}" stroke-width="12"/>'
        f'<circle cx="{size / 2}" cy="{size / 2}" r="{r:.1f}" fill="none" stroke="{color}" stroke-width="12" '
        f'stroke-linecap="round" stroke-dasharray="{c * ratio:.1f} {c:.1f}" transform="rotate(-90 {size / 2} {size / 2})"/>'
        f'<text x="50%" y="48%" text-anchor="middle" class="ring-value">{ratio * 100:.0f}%</text>'
        f'<text x="50%" y="64%" text-anchor="middle" class="ring-label">zajęte</text></svg>'
    )


def hbars(items: list[tuple[str, int, str]], value_fmt=human_size) -> Markup:
    """Ranking (jedna seria): [(etykieta, wartość, link)]."""
    if not items:
        return Markup('<p class="muted">Brak danych.</p>')
    top = max(v for _, v, _ in items) or 1
    rows = []
    for label, v, href in items:
        pct = max(1.5, v / top * 100) if v else 0
        rows.append(
            f'<a class="hbar" href="{escape(href)}" title="{escape(label)}: {escape(value_fmt(v))}">'
            f'<span class="hbar-label">{escape(label)}</span>'
            f'<span class="hbar-track">{_bar_svg(pct, SERIES[0], 10, "#f1f3f6")}</span>'
            f'<span class="hbar-value">{escape(value_fmt(v))}</span></a>'
        )
    return Markup('<div class="hbars">' + "".join(rows) + "</div>")


def sparkline(values: list[int], width: int = 120, height: int = 32) -> Markup:
    if not values or max(values) == 0:
        return Markup(f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" aria-hidden="true">'
                      f'<line x1="0" x2="{width}" y1="{height - 2}" y2="{height - 2}" stroke="{GRID}" stroke-width="2"/></svg>')
    top = max(values)
    step = width / max(1, len(values) - 1)
    pts = " ".join(f"{i * step:.1f},{height - 3 - (v / top) * (height - 8):.1f}" for i, v in enumerate(values))
    last_x, last_y = pts.split()[-1].split(",")
    return Markup(
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" aria-hidden="true">'
        f'<polyline points="{pts}" fill="none" stroke="#b6c3d6" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{last_x}" cy="{last_y}" r="4" fill="{SERIES[0]}" stroke="#fff" stroke-width="2"/></svg>'
    )


def day_range(days: int) -> list[date]:
    today = date.today()
    return [today - timedelta(days=days - 1 - i) for i in range(days)]
