"""Miniaturki i podglądy plików (galeria).

- zdjęcia i grafiki: JPG, PNG, WEBP, GIF, TIFF, BMP, HEIC, PSD (warstwa złożona) — Pillow,
- wideo: klatka z 1. sekundy — ffmpeg,
- PDF i AI (zgodne z PDF): pierwsza strona — pdftoppm.
Wynik: WEBP w dwóch rozmiarach (miniatura do siatki, podgląd do widoku pełnoekranowego),
zapisany w pamięci podręcznej na szybkim dysku. Klucz zawiera sumę pliku, więc nowa wersja = nowa miniatura.
"""
from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("bskcloud.thumbs")

IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif", "tif", "tiff", "bmp", "heic", "heif", "psd"}
VIDEO_EXT = {"mp4", "mov", "m4v", "webm", "mkv", "avi"}
PDF_EXT = {"pdf", "ai"}
BROWSER_VIDEO = {"mp4", "m4v", "webm", "mov"}
BROWSER_AUDIO = {"mp3", "wav", "m4a", "aac", "ogg", "flac"}
SIZES = {"thumb": 520, "preview": 2000}
_sem = threading.Semaphore(2)  # najwyżej 2 jednocześnie — NAS ma ograniczoną pamięć

try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 600_000_000  # duże pliki do druku (np. billboardy)
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:  # noqa: BLE001 — HEIC opcjonalnie
        pass
except Exception:  # noqa: BLE001
    Image = None


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def kind(name: str) -> str:
    """Jak plik pokazać w galerii: image / video / pdf / audio / other."""
    e = ext_of(name)
    if e in IMAGE_EXT:
        return "image"
    if e in VIDEO_EXT:
        return "video"
    if e in PDF_EXT:
        return "pdf"
    if e in BROWSER_AUDIO:
        return "audio"
    return "other"


def can_thumb(name: str) -> bool:
    k = kind(name)
    if k == "image":
        return Image is not None
    if k == "video":
        return shutil.which("ffmpeg") is not None
    if k == "pdf":
        return shutil.which("pdftoppm") is not None and Image is not None
    return False


def plays_in_browser(name: str) -> bool:
    return ext_of(name) in BROWSER_VIDEO | BROWSER_AUDIO


def cache_path(cache_dir: Path, file_id: int, sha: str, size: str) -> Path:
    return cache_dir / f"{file_id}-{(sha or 'x')[:12]}-{size}.webp"


def _save(img, dest: Path, max_px: int) -> None:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("P", "LA") or (img.mode == "RGBA" and dest.suffix == ".jpg"):
        img = img.convert("RGBA")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.stem}.{threading.get_ident()}.tmp")  # osobny plik dla każdego wątku
    img.save(tmp, "WEBP", quality=82, method=4)
    tmp.replace(dest)


def _frame(src: Path, workdir: Path) -> Path | None:
    """Klatka wideo albo 1. strona PDF jako PNG (narzędzia zewnętrzne, z limitem czasu)."""
    k = kind(src.name)
    out = workdir / "frame.png"
    try:
        if k == "video":
            for ss in ("1", "0"):
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", ss, "-i", str(src), "-frames:v", "1",
                                "-vf", "scale=2000:-2:force_original_aspect_ratio=decrease", str(out)],
                               check=False, timeout=60, capture_output=True)
                if out.exists() and out.stat().st_size:
                    return out
        elif k == "pdf":
            subprocess.run(["pdftoppm", "-png", "-r", "110", "-f", "1", "-l", "1", "-singlefile",
                            str(src), str(workdir / "frame")], check=False, timeout=60, capture_output=True)
            if out.exists():
                return out
    except (OSError, subprocess.TimeoutExpired):
        log.warning("Nie udało się odczytać klatki z %s", src.name)
    return None


def generate(src: Path, cache_dir: Path, file_id: int, sha: str) -> bool:
    """Tworzy obie wersje (miniatura + podgląd). Zwraca False, gdy pliku nie da się pokazać."""
    if not can_thumb(src.name) or not src.is_file():
        return False
    targets = {s: cache_path(cache_dir, file_id, sha, s) for s in SIZES}
    if all(p.exists() for p in targets.values()):
        return True
    with _sem:
        try:
            with tempfile.TemporaryDirectory(dir=cache_dir.parent if cache_dir.parent.exists() else None) as tmp:
                source = src if kind(src.name) == "image" else _frame(src, Path(tmp))
                if source is None:
                    return False
                with Image.open(source) as img:
                    if hasattr(img, "draft"):
                        img.draft("RGB", (SIZES["preview"], SIZES["preview"]))  # szybciej dla dużych JPG
                    img.load()
                    for size, dest in sorted(targets.items(), key=lambda kv: -SIZES[kv[0]]):
                        _save(img.copy(), dest, SIZES[size])
            return True
        except Exception:  # noqa: BLE001 — uszkodzony/nietypowy plik: pokażemy ikonę
            log.warning("Brak miniatury dla %s", src.name, exc_info=True)
            return False


_queue: "queue.Queue[tuple]" = queue.Queue()
_workers_started = False
_workers_lock = threading.Lock()


def _worker() -> None:
    while True:
        args = _queue.get()
        try:
            generate(*args)
        except Exception:  # noqa: BLE001
            log.warning("Błąd kolejki miniatur", exc_info=True)
        finally:
            _queue.task_done()


def generate_async(src: Path, cache_dir: Path, file_id: int, sha: str) -> None:
    """Miniatura w tle — przez kolejkę z 2 wątkami (tysiące plików nie tworzą tysięcy wątków)."""
    global _workers_started
    if not can_thumb(src.name):
        return
    with _workers_lock:
        if not _workers_started:
            for _ in range(2):
                threading.Thread(target=_worker, daemon=True, name="miniatury").start()
            _workers_started = True
    _queue.put((src, cache_dir, file_id, sha))


def pending() -> int:
    return _queue.qsize()


def remove(cache_dir: Path, file_id: int) -> None:
    for p in cache_dir.glob(f"{file_id}-*.webp"):
        p.unlink(missing_ok=True)
