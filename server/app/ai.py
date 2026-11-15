"""Lokalny model AI przez Ollamę (kontener „ollama” na NAS, profil docker-compose „ai”).

Bez internetu i bez zewnętrznych usług: zapytania idą do http://ollama:11434. Model na samym procesorze
jest wolny (minuty na odpowiedź), dlatego agent korzysta z niego w tle i z długim limitem czasu.
Brak Ollamy albo modelu = agent działa dalej, tylko bez tekstów pisanych przez AI.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("bskcloud.ai")
_pull_lock = threading.Lock()
pull_state = {"running": False, "status": "", "error": ""}


@dataclass
class Config:
    url: str = "http://ollama:11434"
    model: str = "qwen2.5:14b"
    timeout_s: int = 1800  # procesor NAS: odpowiedź może trwać kilka–kilkanaście minut
    num_ctx: int = 8192


def _post(cfg: Config, path: str, body: dict, timeout: float):
    req = urllib.request.Request(cfg.url.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 — adres z konfiguracji


def status(cfg: Config) -> dict:
    """Czy Ollama odpowiada i czy wybrany model jest pobrany."""
    try:
        with urllib.request.urlopen(cfg.url.rstrip("/") + "/api/tags", timeout=5) as r:  # noqa: S310
            models = [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
    except (OSError, urllib.error.URLError, ValueError):
        return {"online": False, "models": [], "ready": False}
    wanted = cfg.model if ":" in cfg.model else cfg.model + ":latest"
    return {"online": True, "models": models, "ready": wanted in models or cfg.model in models}


def generate(cfg: Config, prompt: str, system: str = "") -> str | None:
    """Odpowiedź modelu (tekst) albo None, gdy model niedostępny."""
    body = {"model": cfg.model, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.3, "num_ctx": cfg.num_ctx}}
    if system:
        body["system"] = system
    try:
        with _post(cfg, "/api/generate", body, cfg.timeout_s) as r:
            text = json.loads(r.read()).get("response", "")
    except (OSError, urllib.error.URLError, ValueError) as exc:
        log.warning("Model AI niedostępny: %s", exc)
        return None
    return _strip_thinking(text).strip() or None


def _strip_thinking(text: str) -> str:
    """Niektóre modele zwracają rozumowanie w <think>…</think> — do e-maila trafia tylko odpowiedź."""
    while "<think>" in text and "</think>" in text:
        start, end = text.index("<think>"), text.index("</think>") + len("</think>")
        text = text[:start] + text[end:]
    return text


def pull(cfg: Config) -> bool:
    """Pobiera model w tle (kilka–kilkanaście GB). Zwraca False, gdy pobieranie już trwa."""
    if not _pull_lock.acquire(blocking=False):
        return False

    def run():
        pull_state.update(running=True, status="łączenie…", error="")
        try:
            with _post(cfg, "/api/pull", {"model": cfg.model, "stream": True}, 6 * 3600) as r:
                for line in r:
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("error"):
                        pull_state["error"] = str(msg["error"])[:300]
                        break
                    done, total = msg.get("completed"), msg.get("total")
                    pull_state["status"] = msg.get("status", "") + (
                        f" {done * 100 // total}%" if done and total else "")
        except (OSError, urllib.error.URLError, ValueError) as exc:
            pull_state["error"] = str(exc)[:300]
        finally:
            pull_state["running"] = False
            _pull_lock.release()

    threading.Thread(target=run, name="pobieranie-modelu", daemon=True).start()
    return True
