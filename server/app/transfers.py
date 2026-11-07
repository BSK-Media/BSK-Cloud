"""Transfery (jak WeTransfer): kilka plików pod jednym linkiem, ZIP w locie, licznik pobrań."""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterable, Iterator

CHUNK = 1024 * 1024


class _Sink:
    """Strumień tylko do zapisu (bez tell/seek) — zipfile używa wtedy deskryptorów danych."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> int:
        self.buf += data
        return len(data)

    def flush(self) -> None:
        pass


def zip_stream(entries: Iterable[tuple[str, Path]]) -> Iterator[bytes]:
    """ZIP generowany kawałek po kawałku (bez kompresji — grafiki/wideo i tak są skompresowane).
    Nie trzyma całego archiwum w pamięci ani na dysku; obsługuje pliki > 4 GB (ZIP64)."""
    sink = _Sink()
    used: set[str] = set()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for arcname, path in entries:
            name, n = arcname, 2
            while name.lower() in used:  # te same nazwy w jednym ZIP-ie
                stem, dot, ext = arcname.rpartition(".")
                name = f"{stem} ({n}).{ext}" if dot else f"{arcname} ({n})"
                n += 1
            used.add(name.lower())
            info = zipfile.ZipInfo.from_file(path, name)
            info.compress_type = zipfile.ZIP_STORED
            with open(path, "rb") as src, z.open(info, "w", force_zip64=True) as dst:
                while data := src.read(CHUNK):
                    dst.write(data)
                    if len(sink.buf) >= CHUNK:
                        yield bytes(sink.buf)
                        sink.buf.clear()
            if sink.buf:
                yield bytes(sink.buf)
                sink.buf.clear()
    if sink.buf:
        yield bytes(sink.buf)


def pick_background(token: str, names: list[str]) -> str | None:
    """To samo tło dla danego transferu przy każdym wejściu."""
    if not names:
        return None
    return sorted(names)[sum(token.encode()) % len(names)]
