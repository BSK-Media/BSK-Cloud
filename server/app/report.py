"""Dzienny raport agenta (e-mail HTML): kafelki, priorytetowe problemy, nowe firmy z oceną, gotowe e-maile."""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone

from . import ai
from .db import Database
from .monitor import SUMMARY_PROMPT, _hhmm

E = html.escape
CARD = "background:#f5f7fb;border-radius:12px;padding:14px 16px"
H3 = "margin:22px 0 10px;font-size:16px"


def collect(db: Database) -> dict:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    sites = db.all("SELECT * FROM sites WHERE active = 1 ORDER BY name, url")
    problems: list[dict] = []
    for s in sites:
        name = s["name"] or s["url"]
        if s["status"] == "down":
            problems.append({"level": "error", "site": name, "text": f"Strona nie działa: {s['last_error'] or 'brak odpowiedzi'}."})
        if s["ssl_expires"]:
            try:
                days = (datetime.fromisoformat(s["ssl_expires"]) - now).days
                if days < 21:
                    problems.append({"level": "error" if days < 10 else "warn", "site": name,
                                     "text": f"Certyfikat SSL wygasa za {days} dni."})
            except ValueError:
                pass
        if s["kind"] == "client":
            integ = json.loads(s["integ"] or "{}") if "integ" in s.keys() else {}
            for i in integ.get("issues", []):
                if i["level"] == "error":
                    problems.append({"level": "error", "site": name, "text": i["text"]})
    nets = db.all("SELECT * FROM net_devices") if db.one("SELECT 1 FROM sqlite_master WHERE name = 'net_devices'") else []
    for d in nets:
        if d["status"] == "down":
            problems.append({"level": "error", "site": f"Sieć: {d['name']}", "text": f"Urządzenie {d['host']} nie odpowiada."})
    problems.sort(key=lambda p: p["level"] != "error")
    has_score = "lead_score" in (sites[0].keys() if sites else [])
    leads = db.all("SELECT s.*, (SELECT COUNT(*) FROM mail_drafts d WHERE d.site_id = s.id AND d.status = 'new') AS drafts"
                   " FROM sites s WHERE s.kind = 'lead' AND s.active = 1 AND s.created_at > ?"
                   + (" ORDER BY s.lead_score DESC" if has_score else ""), since)
    best = db.all("SELECT * FROM sites WHERE kind = 'lead' AND active = 1 AND lead_score IS NOT NULL"
                  " AND id IN (SELECT site_id FROM mail_drafts WHERE status = 'new') ORDER BY lead_score DESC LIMIT 8") \
        if has_score else []
    drafts = db.all("SELECT d.*, s.name, s.url, s.kind FROM mail_drafts d JOIN sites s ON s.id = d.site_id"
                    " WHERE d.status = 'new' ORDER BY d.ts")
    events = db.all("SELECT * FROM agent_events WHERE ts > ? ORDER BY ts", since)
    audits = db.all("SELECT a.*, s.name, s.url FROM site_audits a JOIN sites s ON s.id = a.site_id WHERE a.ts > ?"
                    " ORDER BY a.ts", since)
    return {"sites": sites, "problems": problems, "leads": leads, "best": best, "drafts": drafts,
            "events": events, "audits": audits, "nets": nets}


def build(db: Database, ai_cfg: ai.Config | None, public_url: str, progress=lambda t: None) -> dict:
    c = collect(db)
    log_lines = [f"{_hhmm(e['ts'])} {e['text']}" for e in c["events"]]
    status = (f"Strony: {len(c['sites'])}, problemy do sprawdzenia: {len(c['problems'])}, nowe firmy: {len(c['leads'])}, "
              f"gotowe e-maile: {len(c['drafts'])}.")
    intro = ""
    if ai_cfg and (log_lines or c["problems"]):
        progress("Model AI pisze dzienne podsumowanie")
        prob = [f"PROBLEM {p['site']}: {p['text']}" for p in c["problems"][:20]]
        intro = ai.generate(ai_cfg, SUMMARY_PROMPT.format(log="\n".join([status] + prob + log_lines[-50:]))) or ""
    tiles = [(len(c["sites"]), "stron monitorowanych"), (len(c["problems"]), "problemów do sprawdzenia"),
             (len(c["leads"]), "nowych firm"), (len(c["drafts"]), "gotowych e-maili")]
    h = []
    if intro:
        h.append(f'<p style="margin:0 0 16px;white-space:pre-line">{E(intro.strip())}</p>')
    cells = "".join(
        f'<td width="25%" style="padding:4px"><div style="{CARD}"><div style="font-size:26px;font-weight:700;'
        f'color:{"#d03b3b" if label.startswith("problem") and n else "#111827"}">{n}</div>'
        f'<div style="font-size:12px;color:#6b7280">{E(label)}</div></div></td>' for n, label in tiles)
    h.append(f'<table width="100%" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table>')
    if c["problems"]:
        h.append(f'<h3 style="{H3}">Priorytetowe problemy</h3>')
        for p in c["problems"][:15]:
            icon = "🔴" if p["level"] == "error" else "🟠"
            h.append(f'<p style="margin:0 0 8px">{icon} <b>{E(p["site"])}</b><br><span style="color:#374151">{E(p["text"])}</span></p>')
    else:
        h.append(f'<p style="margin:18px 0 0">✅ Wszystkie strony{" i urządzenia sieci" if c["nets"] else ""} działają, brak pilnych problemów.</p>')
    leads = c["best"] or c["leads"]
    if leads:
        h.append(f'<h3 style="{H3}">Nowe możliwości sprzedażowe</h3><table width="100%" cellpadding="6" cellspacing="0" '
                 'style="border-collapse:collapse;font-size:14px">')
        for s in leads[:10]:
            score = s["lead_score"] if "lead_score" in s.keys() and s["lead_score"] is not None else "—"
            reason = s["lead_reason"] if "lead_reason" in s.keys() else ""
            h.append(f'<tr style="border-top:1px solid #eef0f3"><td><b>{E(s["name"] or s["url"])}</b><br>'
                     f'<span style="color:#6b7280;font-size:12px">{E(s["url"])}{" · " + E(s["source"]) if s["source"] else ""}</span>'
                     f'{"<br><span style=font-size:13px>" + E(reason) + "</span>" if reason else ""}</td>'
                     f'<td align="right" style="white-space:nowrap;font-weight:700">{score}/100</td></tr>')
        h.append("</table>")
    if c["drafts"]:
        h.append(f'<h3 style="{H3}">Gotowe e-maile do wysłania ({len(c["drafts"])})</h3>'
                 f'<p style="margin:0 0 8px;color:#6b7280;font-size:13px">Przejrzyj je w panelu Agent AI i wyślij jednym kliknięciem.</p>')
        for d in c["drafts"][-8:]:
            h.append(f'<div style="{CARD};margin-bottom:8px"><div style="font-size:13px;color:#6b7280">Do: '
                     f'{E(d["to_email"] or "(brak adresu)")} · {E(d["name"] or d["url"])}</div>'
                     f'<b>{E(d["subject"])}</b><div style="white-space:pre-line;font-size:13px;margin-top:6px">'
                     f'{E(d["body"][:700])}{"…" if len(d["body"]) > 700 else ""}</div></div>')
    if c["audits"]:
        h.append(f'<h3 style="{H3}">Audyty z ostatniej doby</h3>')
        for a in c["audits"]:
            h.append(f'<p style="margin:0 0 8px"><b>{E(a["name"] or a["url"])}</b>: {a["score"]}/100'
                     + (f'<br><span style="white-space:pre-line;font-size:13px">{E(a["ai_text"][:900])}</span>' if a["ai_text"] else "")
                     + "</p>")
    if log_lines:
        h.append(f'<h3 style="{H3}">Dziennik agenta</h3><p style="font-size:12px;color:#6b7280;white-space:pre-line;margin:0">'
                 f'{E(chr(10).join(log_lines[-40:]))}</p>')
    text = "\n\n".join(([intro.strip()] if intro else []) + [status]
                       + [f"- {p['site']}: {p['text']}" for p in c["problems"][:15]]
                       + [f"Do: {d['to_email']}\nTemat: {d['subject']}\n\n{d['body']}" for d in c["drafts"][-8:]])
    down = sum(1 for p in c["problems"] if p["level"] == "error")
    subject = (f"Raport AI: {down} pilnych spraw" if down else "Raport AI: wszystko w porządku") \
        + f" ({datetime.now().strftime('%d.%m')})"
    return {"subject": subject, "html": "".join(h), "text": text, "lines": text.split("\n\n")}
