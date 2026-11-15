"""Stan NAS: pamięć RAM, procesor, temperatury, wentylatory, praca dysków i sieć.

Portal działa w kontenerze Dockera. Większość danych w /proc (pamięć, procesor, czas pracy, dyski) i /sys
(czujniki hwmon) i tak dotyczy całego NAS. Dla sieci NAS (a nie kontenera) i nazwy NAS docker-compose
montuje systemowe /proc jako /host/proc (tylko do odczytu) — wtedy ruch czytamy z /host/proc/1/net/dev.
Wszystko jest „najlepszą próbą”: brak czujnika lub pliku = brak sekcji, bez błędu strony.
"""
from __future__ import annotations

import collections
import os
import re
import threading
import time
from pathlib import Path

SAMPLE_S = 5
HISTORY = 120  # 120 × 5 s = ostatnie 10 minut na wykresach
_DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|hd[a-z]+|mmcblk\d+)$")
_SKIP_IF = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "macvlan", "dummy")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


class SysInfo:
    def __init__(self, proc: Path | None = None, sys_dir: Path | None = None, host_proc: Path | None = None):
        self.proc = proc or Path("/proc")
        if sys_dir is None:  # /sys z NAS (zamontowany przez docker-compose) widzi karty sieciowe NAS, nie kontenera
            host_sys = Path(os.environ.get("BSK_HOST_SYS", "/host/sys"))
            sys_dir = host_sys if (host_sys / "class").is_dir() else Path("/sys")
        self.sys = sys_dir
        hp = host_proc if host_proc is not None else Path(os.environ.get("BSK_HOST_PROC", "/host/proc"))
        self.host_proc = hp if (hp / "1" / "net" / "dev").exists() or (hp / "meminfo").exists() else None
        self.history: collections.deque = collections.deque(maxlen=HISTORY)
        self._prev: dict | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.latest_rates: dict = {}

    # ------------------------------------------------------------------ odczyty chwilowe

    def memory(self) -> dict:
        info = {}
        for line in _read(self.proc / "meminfo").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                info[key] = int(parts[0]) * 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        cache = info.get("Cached", 0) + info.get("Buffers", 0) + info.get("SReclaimable", 0)
        return {
            "total": total, "available": avail, "used": max(0, total - avail), "cache": cache,
            "free": info.get("MemFree", 0),
            "swap_total": info.get("SwapTotal", 0), "swap_used": max(0, info.get("SwapTotal", 0) - info.get("SwapFree", 0)),
            "percent": round((total - avail) * 100 / total, 1) if total else 0,
            # struktura jak w Menedżerze zadań UGOS: używana / pamięć podręczna / wolna
            "pct_used": round((total - avail) * 100 / total, 2) if total else 0,
            "pct_cache": round(min(cache, avail) * 100 / total, 2) if total else 0,
            "remaining": max(0, avail - min(cache, avail)),
        }

    def cpu(self) -> dict:
        model, cores, mhz = "", 0, []
        for line in _read(self.proc / "cpuinfo").splitlines():
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            if key == "model name" and not model:
                model = re.sub(r"\s+", " ", val)
            elif key == "processor":
                cores += 1
            elif key == "cpu MHz":
                try:
                    mhz.append(float(val))
                except ValueError:
                    pass
        load = _read(self.proc / "loadavg").split()
        return {"model": model, "cores": cores or (os.cpu_count() or 0),
                "mhz": round(sum(mhz) / len(mhz)) if mhz else 0,
                "load": [float(x) for x in load[:3]] if len(load) >= 3 else []}

    def uptime_s(self) -> int:
        parts = _read(self.proc / "uptime").split()
        try:
            return int(float(parts[0]))
        except (IndexError, ValueError):
            return 0

    def hostname(self) -> str:
        for p in (Path(os.environ.get("BSK_HOST_HOSTNAME", "/host/hostname")), self.proc / "sys" / "kernel" / "hostname"):
            name = _read(p).strip()
            if name:
                return name
        return ""

    def kernel(self) -> str:
        return _read(self.proc / "sys" / "kernel" / "osrelease").strip() or os.uname().release

    def sensors(self) -> dict:
        """Czujniki hwmon: temperatury (°C) i wentylatory (obr./min) z czytelnymi nazwami."""
        temps, fans = [], []
        base = self.sys / "class" / "hwmon"
        try:
            chips = sorted(base.iterdir(), key=lambda p: p.name)
        except OSError:
            chips = []
        for chip in chips:
            name = _read(chip / "name").strip() or chip.name
            device = ""
            dev_model = _read(chip / "device" / "model").strip()  # drivetemp: model dysku
            if dev_model:
                device = dev_model
            disk = ""
            for pattern in ("device/block/*", "device/nvme*n*", "device/device/block/*"):
                found = sorted(chip.glob(pattern))
                if found:
                    disk = found[0].name
                    break
            for f in sorted(chip.glob("temp*_input")):
                raw = _read(f).strip()
                if not raw.lstrip("-").isdigit():
                    continue
                value = int(raw) / 1000
                if value <= -40 or value >= 150:
                    continue
                label = _read(f.with_name(f.name.replace("_input", "_label"))).strip()
                crit = _read(f.with_name(f.name.replace("_input", "_crit"))).strip()
                temps.append({"chip": name, "label": _pretty_temp(name, label, device or (f"{disk}" if disk else "")),
                              "value": round(value, 1), "crit": int(crit) / 1000 if crit.isdigit() else None,
                              "disk": disk if (disk and (not label or label == "Composite" or "temp1" in f.name)) else ""})
            for f in sorted(chip.glob("fan*_input")):
                raw = _read(f).strip()
                if raw.isdigit():
                    label = _read(f.with_name(f.name.replace("_input", "_label"))).strip()
                    fans.append({"label": label or f"Wentylator {f.name[3:-6]}", "rpm": int(raw)})
        if not temps:  # część systemów ma tylko thermal_zone
            for zone in sorted((self.sys / "class" / "thermal").glob("thermal_zone*")):
                raw = _read(zone / "temp").strip()
                if raw.lstrip("-").isdigit() and -40 < int(raw) / 1000 < 150:
                    temps.append({"chip": "thermal", "label": _read(zone / "type").strip() or zone.name,
                                  "value": round(int(raw) / 1000, 1), "crit": None})
        return {"temps": temps, "fans": fans}

    def _net_counters(self) -> dict[str, tuple[int, int]]:
        text = _read(self.host_proc / "1" / "net" / "dev") if self.host_proc else ""
        text = text or _read(self.proc / "net" / "dev")  # bez /host/proc: ruch samego kontenera
        out = {}
        for line in text.splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            vals = rest.split()
            if not name or name.startswith(_SKIP_IF) or len(vals) < 9:
                continue
            out[name] = (int(vals[0]), int(vals[8]))
        return out

    def _disk_counters(self) -> dict[str, tuple[int, int, int]]:
        out = {}
        for line in _read(self.proc / "diskstats").splitlines():
            p = line.split()
            if len(p) >= 13 and _DISK_RE.match(p[2]):
                # sektory odczytane / zapisane, czas pracy dysku (ms) -> obciążenie %
                out[p[2]] = (int(p[5]) * 512, int(p[9]) * 512, int(p[12]))
        return out

    def _cpu_times(self) -> tuple[int, int]:
        first = _read(self.proc / "stat").splitlines()[:1]
        if not first or not first[0].startswith("cpu "):
            return 0, 0
        vals = [int(x) for x in first[0].split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return sum(vals), idle

    def net_info(self) -> dict[str, dict]:
        """Karty sieciowe NAS: prędkość łącza, czy podłączone, nazwa jak w UGOS (LAN 1, LAN 2…)."""
        out = {}
        names = sorted(self._net_counters())
        physical = [n for n in names if (self.sys / "class" / "net" / n / "device").exists()]
        for i, name in enumerate(physical or names, 1):
            base = self.sys / "class" / "net" / name
            speed = _read(base / "speed").strip()
            out[name] = {"label": f"LAN {i}" if physical else name,
                         "speed": int(speed) if speed.isdigit() and int(speed) > 0 else None,
                         "up": _read(base / "operstate").strip() == "up"}
        return out

    def disks_info(self, temps: list[dict]) -> dict[str, dict]:
        """Dyski: model, pojemność, HDD/SSD/NVMe i temperatura (czujnik drivetemp lub nvme)."""
        by_disk = {t["disk"]: t["value"] for t in temps if t.get("disk")}
        out = {}
        for name in self._disk_counters():
            base = self.sys / "block" / name
            size = _read(base / "size").strip()
            size_b = int(size) * 512 if size.isdigit() else 0
            if size_b == 0:
                continue
            rot = _read(base / "queue" / "rotational").strip()
            kind = "NVMe" if name.startswith("nvme") else ("HDD" if rot == "1" else "SSD")
            model = (_read(base / "device" / "model").strip() or _read(base / "device" / "device" / "model").strip())
            out[name] = {"model": model, "size": size_b, "kind": kind, "temp": by_disk.get(name)}
        return out

    # ------------------------------------------------------------------ próbki (prędkości i historia)

    def sample(self) -> dict:
        now = time.monotonic()
        cur = {"t": now, "cpu": self._cpu_times(), "net": self._net_counters(), "disk": self._disk_counters()}
        rates = {"cpu": None, "net": {}, "disk": {}}
        with self._lock:
            prev = self._prev
            self._prev = cur
            if prev:
                dt = max(0.001, now - prev["t"])
                total = cur["cpu"][0] - prev["cpu"][0]
                idle = cur["cpu"][1] - prev["cpu"][1]
                rates["cpu"] = round(max(0.0, min(100.0, (total - idle) * 100 / total)), 1) if total > 0 else 0.0
                for name, (rx, tx) in cur["net"].items():
                    if name in prev["net"]:
                        prx, ptx = prev["net"][name]
                        rates["net"][name] = (max(0, rx - prx) / dt, max(0, tx - ptx) / dt)
                for name, (rd, wr, busy) in cur["disk"].items():
                    if name in prev["disk"]:
                        prd, pwr, pbusy = prev["disk"][name]
                        util = min(100.0, max(0, busy - pbusy) / (dt * 10))  # ms pracy / ms czasu * 100
                        rates["disk"][name] = (max(0, rd - prd) / dt, max(0, wr - pwr) / dt, round(util, 1))
                mem = self.memory()
                temps = self.sensors()["temps"]
                cpu_temp = _cpu_temperature(temps)
                self.history.append({
                    "ts": time.time(), "cpu": rates["cpu"], "mem": mem["percent"],
                    "rx": sum(r[0] for r in rates["net"].values()), "tx": sum(r[1] for r in rates["net"].values()),
                    "read": sum(r[0] for r in rates["disk"].values()), "write": sum(r[1] for r in rates["disk"].values()),
                    "temp": cpu_temp,
                })
                self.latest_rates = rates
        return rates

    def start(self) -> None:
        """Próbki co 5 s w tle (raz na proces)."""
        with self._lock:
            if self._thread:
                return
            self._thread = threading.Thread(target=self._loop, name="stan-nas", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                self.sample()
            except Exception:  # noqa: BLE001 — statystyki nie mogą zatrzymać portalu
                pass
            time.sleep(SAMPLE_S)

    def snapshot(self) -> dict:
        if not self.latest_rates:
            self.sample()
            time.sleep(0.5)
            self.sample()
        mem, cpu, sens = self.memory(), self.cpu(), self.sensors()
        rates = self.latest_rates or {"cpu": None, "net": {}, "disk": {}}
        nets = self.net_info()
        dinfo = self.disks_info(sens["temps"])
        return {
            "hostname": self.hostname(), "kernel": self.kernel(), "uptime": self.uptime_s(),
            "memory": mem, "cpu": {**cpu, "usage": rates.get("cpu")}, "temps": sens["temps"], "fans": sens["fans"],
            "cpu_temp": _cpu_temperature(sens["temps"]),
            "net": [{"name": n, "rx": r[0], "tx": r[1], **nets.get(n, {"label": n, "speed": None, "up": True})}
                    for n, r in sorted(rates.get("net", {}).items(), key=lambda kv: nets.get(kv[0], {}).get("label", kv[0]))],
            "disks": [{"name": n, "read": r[0], "write": r[1], "util": r[2], **dinfo[n]}
                      for n, r in sorted(rates.get("disk", {}).items()) if n in dinfo],
            "history": list(self.history), "host_view": self.host_proc is not None,
        }


def _pretty_temp(chip: str, label: str, device: str) -> str:
    if device:
        return f"Dysk {device}"
    names = {"coretemp": "Procesor", "k10temp": "Procesor", "zenpower": "Procesor", "cpu_thermal": "Procesor",
             "nvme": "Dysk NVMe", "drivetemp": "Dysk", "acpitz": "Płyta główna", "pch_": "Chipset",
             "it87": "Płyta główna", "nct": "Płyta główna", "iwlwifi": "Wi-Fi", "r8169": "Karta sieciowa",
             "atlantic": "Karta sieciowa 10G", "aqc": "Karta sieciowa 10G", "spd5118": "Pamięć RAM",
             "jc42": "Pamięć RAM"}
    base = next((v for k, v in names.items() if chip.startswith(k)), chip)
    if label:
        label = label.replace("Package id 0", "pakiet").replace("Composite", "").replace("Core ", "rdzeń ").strip()
    return f"{base} — {label}" if label else base


def _cpu_temperature(temps: list[dict]) -> float | None:
    for t in temps:
        if t["chip"] in ("coretemp", "k10temp", "zenpower", "cpu_thermal") and ("pakiet" in t["label"] or "Tctl" in t["label"]):
            return t["value"]
    cpu = [t["value"] for t in temps if t["chip"] in ("coretemp", "k10temp", "zenpower", "cpu_thermal")]
    if cpu:
        return max(cpu)
    zone = [t["value"] for t in temps if t["chip"] in ("thermal", "acpitz")]
    return max(zone) if zone else None


def fmt_rate(bps: float) -> str:
    """B/s -> czytelna prędkość (MB/s, KB/s)."""
    for unit, size in (("GB/s", 1024 ** 3), ("MB/s", 1024 ** 2), ("KB/s", 1024)):
        if bps >= size:
            return f"{bps / size:.1f} {unit}"
    return f"{bps:.0f} B/s"


def fmt_uptime(seconds: int) -> str:
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} {'dzień' if d == 1 else 'dni'} {h} godz."
    if h:
        return f"{h} godz. {m} min"
    return f"{m} min"
