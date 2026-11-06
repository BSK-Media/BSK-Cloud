"""Edycja dokumentów online (Excel, Word, PowerPoint) przez ONLYOFFICE Docs na tym samym NAS.

Kilka osób (np. admin i klient) może edytować ten sam plik jednocześnie i widzi zmiany na żywo.
Serwer dokumentów pobiera plik z portalu (podpisany adres wewnętrzny) i po zamknięciu edytora
odsyła nową wersję na adres zwrotny (callback). Konfiguracja edytora jest podpisana JWT (HS256).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import time
import zipfile

EDITABLE = {
    "xlsx": "cell", "ods": "cell", "csv": "cell",
    "docx": "word", "odt": "word", "txt": "word", "rtf": "word",
    "pptx": "slide", "odp": "slide",
}
VIEW_ONLY = {"xls": "cell", "doc": "word", "ppt": "slide", "pdf": "pdf"}


def doc_type(filename: str) -> tuple[str, str, bool] | None:
    """(rozszerzenie, typ dokumentu, czy można edytować) albo None, gdy plik nie jest dokumentem."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in EDITABLE:
        return ext, EDITABLE[ext], True
    if ext in VIEW_ONLY:
        return ext, VIEW_ONLY[ext], False
    return None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def jwt_encode(payload: dict, secret: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


def jwt_decode(token: str, secret: str) -> dict:
    try:
        header, body, sig = token.split(".")
    except ValueError:
        raise ValueError("Nieprawidłowy token")
    expected = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _unb64(sig)):
        raise ValueError("Nieprawidłowy podpis")
    if json.loads(_unb64(header)).get("alg") != "HS256":
        raise ValueError("Nieobsługiwany algorytm")
    payload = json.loads(_unb64(body))
    if "exp" in payload and payload["exp"] < time.time():
        raise ValueError("Token wygasł")
    return payload


def sign_url(secret: str, file_id: int, purpose: str, ttl_s: int = 24 * 3600) -> str:
    """Podpis dla adresów, które odwiedza tylko serwer dokumentów (pobranie pliku, callback)."""
    exp = int(time.time()) + ttl_s
    mac = hmac.new(secret.encode(), f"{purpose}:{file_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{mac}"


def check_url(secret: str, file_id: int, purpose: str, sig: str) -> bool:
    try:
        exp_s, mac = sig.split(".")
        exp = int(exp_s)
    except ValueError:
        return False
    expected = hmac.new(secret.encode(), f"{purpose}:{file_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return exp >= time.time() and hmac.compare_digest(expected, mac)


def editor_config(*, secret: str, file_row, user, internal_app_url: str, mode: str, lang: str = "pl") -> dict:
    ext, dtype, editable = doc_type(file_row["original_name"])
    fid = file_row["id"]
    config = {
        "document": {
            "fileType": ext,
            # klucz zmienia się po każdym zapisie — wtedy kolejne otwarcie ładuje nową wersję
            "key": f"bsk-{fid}-{file_row['office_version']}",
            "title": file_row["original_name"],
            "url": f"{internal_app_url}/office/file/{fid}?sig={sign_url(secret, fid, 'file')}",
            "permissions": {"edit": editable and mode == "edit", "download": True, "print": True,
                            "comment": True, "review": True},
        },
        "documentType": dtype,
        "editorConfig": {
            "mode": "edit" if editable and mode == "edit" else "view",
            "lang": lang,
            "region": "pl-PL",
            "callbackUrl": f"{internal_app_url}/office/callback/{fid}?sig={sign_url(secret, fid, 'callback', 30 * 24 * 3600)}",
            "user": {"id": str(user["id"]), "name": user["display_name"] or user["username"]},
            "customization": {"autosave": True, "forcesave": True, "compactHeader": False,
                              "goback": False, "help": False, "feedback": False},
            "coEditing": {"mode": "fast", "change": False},
        },
        "height": "100%",
        "width": "100%",
        "type": "desktop",
    }
    config["token"] = jwt_encode({k: v for k, v in config.items() if k != "token"}, secret)
    return config


# ---------------------------------------------------------------- puste dokumenty (nowy arkusz / dokument)

_CT_XLSX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
_RELS_XLSX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
_WB = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Arkusz1" sheetId="1" r:id="rId1"/></sheets></workbook>"""
_WB_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
_SHEET = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>"""

_CT_DOCX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
_RELS_DOCX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
_DOC = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>"""


def blank_document(kind: str) -> tuple[bytes, str]:
    """Zwraca (zawartość, rozszerzenie) pustego arkusza ('xlsx') lub dokumentu ('docx')."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if kind == "xlsx":
            z.writestr("[Content_Types].xml", _CT_XLSX)
            z.writestr("_rels/.rels", _RELS_XLSX)
            z.writestr("xl/workbook.xml", _WB)
            z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
            z.writestr("xl/worksheets/sheet1.xml", _SHEET)
        elif kind == "docx":
            z.writestr("[Content_Types].xml", _CT_DOCX)
            z.writestr("_rels/.rels", _RELS_DOCX)
            z.writestr("word/document.xml", _DOC)
        else:
            raise ValueError("Nieznany typ dokumentu")
    return buf.getvalue(), kind
