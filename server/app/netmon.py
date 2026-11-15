"""Sieć biurowa: router, switche, punkty dostępowe, NAS.

- dostępność: ping (ICMP bez uprawnień administratora; zapasowo połączenie TCP na port 443/80/22),
- SNMP v2c (opcjonalnie, community np. „public” — w Ruijie: Obszar roboczy → SNMP): nazwa urządzenia,
  czas pracy (wykrywa restart), stan portów (porty, które przestały działać).
Prosty klient SNMP w czystym Pythonie (GET / GETNEXT), bez dodatkowych bibliotek.
"""
from __future__ import annotations

import os
import random
import socket
import struct
import time

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_OPER = "1.3.6.1.2.1.2.2.1.8"


# ------------------------------------------------------------------ ping

def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def ping(host: str, timeout: float = 2.0) -> float | None:
    """Czas odpowiedzi w ms albo None. Najpierw ICMP (gniazdo bez uprawnień root), potem TCP."""
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP) as s:
            s.settimeout(timeout)
            ident, seq = os.getpid() & 0xFFFF, random.randint(0, 0xFFFF)
            payload = b"bsk-ping" + struct.pack("!d", time.time())
            hdr = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
            pkt = struct.pack("!BBHHH", 8, 0, _checksum(hdr + payload), ident, seq) + payload
            start = time.monotonic()
            s.sendto(pkt, (ip, 0))
            while time.monotonic() - start < timeout:
                data, _ = s.recvfrom(1024)
                if data and data[0] == 0:  # echo reply
                    return round((time.monotonic() - start) * 1000, 1)
    except PermissionError:
        pass  # system nie pozwala na ICMP bez uprawnień — sprawdź porty TCP
    except OSError:
        return None
    for port in (443, 80, 22, 8443):
        start = time.monotonic()
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return round((time.monotonic() - start) * 1000, 1)
        except ConnectionRefusedError:
            return round((time.monotonic() - start) * 1000, 1)  # odrzucenie = urządzenie odpowiada
        except OSError:
            continue
    return None


# ------------------------------------------------------------------ SNMP v2c (BER)

def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def _int(n: int) -> bytes:
    return _tlv(0x02, n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=True))


def _oid(oid: str) -> bytes:
    parts = [int(x) for x in oid.split(".")]
    out = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        enc = [p & 0x7F]
        p >>= 7
        while p:
            enc.insert(0, 0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(enc)
    return _tlv(0x06, out)


def encode_request(community: str, oids: list[str], pdu: int = 0xA0, req_id: int = 1) -> bytes:
    varbinds = b"".join(_tlv(0x30, _oid(o) + b"\x05\x00") for o in oids)
    body = _int(req_id) + _int(0) + _int(0) + _tlv(0x30, varbinds)
    return _tlv(0x30, _int(1) + _tlv(0x04, community.encode()) + _tlv(pdu, body))


def _read(data: bytes, pos: int) -> tuple[int, bytes, int]:
    tag = data[pos]
    ln = data[pos + 1]
    pos += 2
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    return tag, data[pos:pos + ln], pos + ln


def _decode_oid(v: bytes) -> str:
    parts = [v[0] // 40, v[0] % 40]
    n = 0
    for b in v[1:]:
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(n)
            n = 0
    return ".".join(map(str, parts))


def _value(tag: int, v: bytes):
    if tag == 0x02 or tag in (0x41, 0x42, 0x43, 0x46):
        return int.from_bytes(v, "big", signed=tag == 0x02)
    if tag == 0x04:
        return v.decode("utf-8", errors="replace")
    if tag == 0x06:
        return _decode_oid(v)
    return None  # NULL, noSuchObject, endOfMibView


def decode_response(data: bytes) -> tuple[int, list[tuple[str, object, int]]]:
    """(error_status, [(oid, wartość, tag)])"""
    _, msg, _ = _read(data, 0)
    pos = 0
    _, _, pos = _read(msg, pos)  # version
    _, _, pos = _read(msg, pos)  # community
    _, pdu, _ = _read(msg, pos)
    p = 0
    _, _, p = _read(pdu, p)  # request id
    _, err, p = _read(pdu, p)
    _, _, p = _read(pdu, p)  # error index
    _, vbs, _ = _read(pdu, p)
    out, q = [], 0
    while q < len(vbs):
        _, vb, q = _read(vbs, q)
        _, oid_raw, r = _read(vb, 0)
        tag, val, _ = _read(vb, r)
        out.append((_decode_oid(oid_raw), _value(tag, val), tag))
    return int.from_bytes(err, "big"), out


def _snmp(host: str, community: str, oids: list[str], pdu: int, timeout: float, port: int):
    req_id = random.randint(1, 2**30)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        for _ in range(2):
            s.sendto(encode_request(community, oids, pdu, req_id), (host, port))
            try:
                data, _ = s.recvfrom(65535)
                return decode_response(data)[1]
            except socket.timeout:
                continue
    raise TimeoutError("SNMP: brak odpowiedzi")


def snmp_get(host: str, community: str, oids: list[str], timeout: float = 2.0, port: int = 161) -> dict:
    return {oid: val for oid, val, tag in _snmp(host, community, oids, 0xA0, timeout, port) if tag < 0x80}


def snmp_walk(host: str, community: str, base: str, limit: int = 128, timeout: float = 2.0, port: int = 161) -> dict:
    out, cur = {}, base
    for _ in range(limit):
        rows = _snmp(host, community, [cur], 0xA1, timeout, port)
        if not rows:
            break
        oid, val, tag = rows[0]
        if tag >= 0x80 or not oid.startswith(base + "."):
            break
        out[oid[len(base) + 1:]] = val
        cur = oid
    return out


def device_status(host: str, community: str = "", timeout: float = 2.0, port: int = 161) -> dict:
    """Stan urządzenia: ping, a z SNMP także nazwa, czas pracy (sekundy) i porty."""
    out = {"ms": ping(host, timeout), "snmp": False, "name": "", "uptime_s": None, "ports": {}, "error": ""}
    if community:
        try:
            info = snmp_get(host, community, [SYS_NAME, SYS_UPTIME, SYS_DESCR], timeout, port)
            out["snmp"] = True
            out["name"] = str(info.get(SYS_NAME, ""))[:120]
            ticks = info.get(SYS_UPTIME)
            out["uptime_s"] = int(ticks) // 100 if isinstance(ticks, int) else None
            names = snmp_walk(host, community, IF_DESCR, timeout=timeout, port=port)
            oper = snmp_walk(host, community, IF_OPER, timeout=timeout, port=port)
            out["ports"] = {str(names.get(i, f"port {i}")): ("up" if v == 1 else "down") for i, v in oper.items()}
            if out["ms"] is None:
                out["ms"] = 0.0  # SNMP odpowiada, więc urządzenie działa (ping może być blokowany)
        except (OSError, TimeoutError, IndexError, ValueError) as exc:
            out["error"] = f"SNMP: {exc}"[:200]
    return out
