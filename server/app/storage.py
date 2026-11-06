"""Bezpieczne nazwy plików i ścieżek oraz zapis strumieniowy z sumą SHA-256."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

_FORBIDDEN = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
CHUNK = 1024 * 1024


class TooLarge(Exception):
    pass


def safe_name(name: str, fallback: str = "plik") -> str:
    """Pojedynczy segment ścieżki: bez separatorów, znaków sterujących i nazw zastrzeżonych."""
    name = name.replace("\\", "/").split("/")[-1]
    name = _FORBIDDEN.sub("_", name).strip().rstrip(". ")
    if not name or set(name) == {"."}:
        name = fallback
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if len(name) > 180:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 180 - len(ext) - 1] + "." + ext) if dot and len(ext) < 20 else name[:180]
    return name


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-.")
    return slug[:60] or "konto"


def safe_rel_path(raw: str) -> str:
    """Ścieżka względna od agenta (np. 'Pulpit/projekt/plik.psd') -> bezpieczna postać."""
    parts = [p for p in raw.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or len(parts) > 64:
        raise ValueError("Nieprawidłowa ścieżka")
    clean = [safe_name(p, "_") for p in parts]
    if clean[0].startswith(".wersje"):
        clean[0] = "_" + clean[0]
    return "/".join(clean)


def inside(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    i = 2
    while True:
        candidate = directory / (f"{stem} ({i}).{ext}" if ext else f"{stem} ({i})")
        if not candidate.exists():
            return candidate
        i += 1


def copy_hashed(src: BinaryIO, dest: Path, tmp_dir: Path, max_bytes: int) -> tuple[int, str]:
    """Kopiuje strumień do pliku tymczasowego, liczy SHA-256, na końcu atomowo przenosi."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, prefix="up-")
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise TooLarge()
                digest.update(chunk)
                out.write(chunk)
        shutil.move(tmp_name, dest)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return size, digest.hexdigest()


def version_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def prune_versions(versions_dir: Path, base_name: str, keep: int) -> int:
    if not versions_dir.is_dir():
        return 0
    prefix = base_name + "."
    found = sorted(
        (p for p in versions_dir.iterdir() if p.is_file() and p.name.startswith(prefix)),
        key=lambda p: p.name,
    )
    removed = 0
    for old in found[: max(0, len(found) - keep)]:
        old.unlink(missing_ok=True)
        removed += 1
    return len(found) - removed


def human_size(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
