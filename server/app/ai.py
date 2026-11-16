"""Lokalny model AI przez Ollamę (kontener „ollama” na NAS, profil docker-compose „ai”).

Bez internetu i bez zewnętrznych usług: zapytania idą do http://ollama:11434. Model na samym procesorze
jest wolny (minuty na odpowiedź), dlatego agent korzysta z niego w tle i z długim limitem czasu.
Brak Ollamy albo modelu = agent działa dalej, tylko bez tekstów pisanych przez AI.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("bskcloud.ai")
_pull_lock = threading.Lock()
pull_state = {"running": False, "status": "", "error": ""}


# Propozycje w panelu: (nazwa w Ollamie, opis). Na samym procesorze szybkość zależy od liczby parametrów
# AKTYWNYCH przy każdym słowie — modele MoE (wielu „ekspertów”, aktywna tylko część) zajmują dużo RAM,
# a piszą prawie tak szybko jak małe modele.
MODELS = [
    ("qwen3.8:27b", "Qwen3.8 27B — zalecany: najnowsza generacja, najlepiej trzyma się instrukcji, rozumie też obrazy (ok. 19 GB RAM)"),
    ("qwen3.8:27b-q8_0", "Qwen3.8 27B Q8 — ten sam model w wyższej precyzji (ok. 30 GB RAM), jeśli Ollama udostępnia taką wersję"),
    ("qwen3.6:27b", "Qwen3.6 27B — poprzednia generacja (ok. 19 GB RAM)"),
    ("qwen3:32b-q8_0", "Qwen3 32B Q8 — ok. 35 GB RAM, starsza generacja"),
    ("qwen3:32b", "Qwen3 32B Q4 — ok. 20 GB RAM"),
    ("qwen2.5:72b", "Qwen2.5 72B — ok. 47 GB RAM, starsza generacja, bardzo dobry polski, wolny"),
    ("llama3.3:70b", "Llama 3.3 70B — ok. 43 GB RAM, dużo wiedzy, wolny"),
    ("qwen3:30b", "Qwen3 30B (MoE) — ok. 19 GB RAM, kilka razy szybszy, dobry polski"),
    ("gpt-oss:20b", "gpt-oss 20B (MoE) — ok. 14 GB RAM, najszybszy, polski poprawny"),
    ("gemma3:27b", "Gemma 3 27B — ok. 17 GB RAM, bardzo dobry polski, kilka razy wolniejszy"),
    ("qwen2.5:14b", "Qwen2.5 14B — ok. 9 GB RAM, poprzedni domyślny"),
]


@dataclass
class Config:
    url: str = "http://ollama:11434"
    model: str = "qwen3.8:27b"
    timeout_s: int = 3 * 3600  # duży model na procesorze NAS: jedna odpowiedź może trwać kilkadziesiąt minut
    num_ctx: int = 16384  # miejsce na „myślenie” modelu i dłuższe strony


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


def generate(cfg: Config, prompt: str, system: str = "", images: list[bytes] | None = None) -> str | None:
    """Odpowiedź modelu (tekst) albo None, gdy model niedostępny (albo nie rozpoznaje obrazów)."""
    body = {"model": cfg.model, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.3, "num_ctx": cfg.num_ctx}}
    if images:
        body["images"] = [base64.b64encode(i).decode() for i in images]
    if system:
        body["system"] = system
    think = _think_setting(cfg.model)
    if think is not None:
        body["think"] = think  # model rozważa odpowiedź przed napisaniem (rozumowanie nie trafia do tekstu)
    try:
        try:
            with _post(cfg, "/api/generate", body, cfg.timeout_s) as r:
                text = json.loads(r.read()).get("response", "")
        except urllib.error.HTTPError as exc:
            if exc.code != 400 or "think" not in body:
                raise
            body.pop("think")  # starsza Ollama albo model bez tej opcji
            with _post(cfg, "/api/generate", body, cfg.timeout_s) as r:
                text = json.loads(r.read()).get("response", "")
    except (OSError, urllib.error.URLError, ValueError) as exc:
        log.warning("Model AI niedostępny: %s", exc)
        return None
    return _strip_thinking(text).strip() or None


def _think_setting(model: str):
    """Jakość ponad szybkość: modele z „myśleniem” rozważają odpowiedź przed napisaniem (wolniej, ale lepiej)."""
    name = model.lower()
    if name.startswith(("qwen3", "deepseek-r1")):
        return True
    if name.startswith("gpt-oss"):
        return "high"
    return None


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
