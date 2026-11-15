"""Stan NAS na sztucznych /proc i /sys (jak w kontenerze z zamontowanym /host/proc i /host/sys)."""
from pathlib import Path

from app import sysinfo


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fake_nas(tmp: Path, step: int = 0) -> tuple[Path, Path, Path]:
    proc, sys_dir, host = tmp / "proc", tmp / "sys", tmp / "host"
    gb = 1024 * 1024  # kB
    _w(proc / "meminfo", f"MemTotal: {64 * gb} kB\nMemFree: {50 * gb} kB\nMemAvailable: {60 * gb} kB\n"
                         f"Buffers: {gb // 2} kB\nCached: {4 * gb} kB\nSReclaimable: {gb // 2} kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    _w(proc / "cpuinfo", "".join(f"processor\t: {i}\nmodel name\t: Intel(R) Pentium(R) Gold 8505\ncpu MHz\t\t: 1200.000\n\n"
                                 for i in range(6)))
    _w(proc / "loadavg", "0.12 0.20 0.31 1/300 1234\n")
    _w(proc / "uptime", "200000.5 100.0\n")
    _w(proc / "sys" / "kernel" / "osrelease", "6.12.30\n")
    busy = 100 * step
    _w(proc / "stat", f"cpu  {1000 + busy} 0 {500} {10000 + 400 * step} 0 0 0 0 0 0\n")
    _w(proc / "diskstats", f"   8       0 sda 100 0 {2048 * (1 + step)} 0 50 0 {4096 * (1 + 2 * step)} 0 0 {1000 + 2500 * step} 0\n"
                           "   8       1 sda1 1 0 8 0 0 0 0 0 0 0 0\n"
                           "   7       0 loop0 1 0 8 0 0 0 0 0 0 0 0\n")
    # ruch NAS widziany przez /host/proc/1/net/dev (kontener ma tylko własne eth0)
    _w(host / "1" / "net" / "dev", "Inter-|   Receive\n face |bytes\n"
       f"    lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0\n"
       f"  eth0: {1000000 + 5_000_000 * step} 0 0 0 0 0 0 0 {2000000 + 1_000_000 * step} 0 0 0 0 0 0 0\n"
       f"  eth1: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
       f"docker0: 5 0 0 0 0 0 0 0 5 0 0 0 0 0 0 0\n")
    _w(proc / "net" / "dev", "x\ny\n  eth0: 1 0 0 0 0 0 0 0 1 0 0 0 0 0 0 0\n")
    for name, speed, state in (("eth0", "10000", "up"), ("eth1", "-1", "down")):
        _w(sys_dir / "class" / "net" / name / "speed", speed)
        _w(sys_dir / "class" / "net" / name / "operstate", state)
        (sys_dir / "class" / "net" / name / "device").mkdir(parents=True, exist_ok=True)
    hw = sys_dir / "class" / "hwmon"
    _w(hw / "hwmon0" / "name", "coretemp")
    _w(hw / "hwmon0" / "temp1_input", "41000")
    _w(hw / "hwmon0" / "temp1_label", "Package id 0")
    _w(hw / "hwmon0" / "temp2_input", "39000")
    _w(hw / "hwmon0" / "temp2_label", "Core 0")
    _w(hw / "hwmon1" / "name", "drivetemp")
    _w(hw / "hwmon1" / "temp1_input", "30000")
    _w(hw / "hwmon1" / "device" / "model", "ST10000VN000")
    (hw / "hwmon1" / "device" / "block" / "sda").mkdir(parents=True, exist_ok=True)
    _w(hw / "hwmon2" / "name", "it87")
    _w(hw / "hwmon2" / "fan1_input", "418")
    _w(sys_dir / "block" / "sda" / "size", str(9 * 1024 ** 4 // 512))
    _w(sys_dir / "block" / "sda" / "queue" / "rotational", "1")
    _w(sys_dir / "block" / "sda" / "device" / "model", "ST10000VN000")
    return proc, sys_dir, host


def test_snapshot_reads_nas(tmp_path, monkeypatch):
    proc, sys_dir, host = fake_nas(tmp_path)
    nas = sysinfo.SysInfo(proc=proc, sys_dir=sys_dir, host_proc=host)
    nas.sample()
    nas._prev["t"] -= 5  # pierwsza próbka 5 s temu
    fake_nas(tmp_path, step=1)
    nas.sample()
    s = nas.snapshot()
    m = s["memory"]
    assert m["total"] == 64 * 1024 ** 3 and round(m["percent"]) == 6
    assert m["pct_used"] + m["pct_cache"] <= 100 and m["remaining"] > 0
    assert s["cpu"]["cores"] == 6 and "Pentium" in s["cpu"]["model"] and s["cpu"]["load"] == [0.12, 0.2, 0.31]
    assert s["cpu"]["usage"] == 20.0  # 100 zajętych z 500 jednostek czasu
    assert s["cpu_temp"] == 41.0 and s["uptime"] == 200000
    assert s["fans"] == [{"label": "Wentylator 1", "rpm": 418}]
    nets = {n["name"]: n for n in s["net"]}
    assert set(nets) == {"eth0", "eth1"}  # bez lo i docker0, dane z /host/proc (NAS), nie kontenera
    assert nets["eth0"]["label"] == "LAN 1" and nets["eth0"]["speed"] == 10000 and nets["eth1"]["up"] is False
    assert 900_000 < nets["eth0"]["rx"] < 1_100_000 and 150_000 < nets["eth0"]["tx"] < 250_000  # ~1 MB/s, ~0,2 MB/s
    disk = s["disks"][0]
    assert disk["name"] == "sda" and disk["kind"] == "HDD" and disk["temp"] == 30.0 and disk["model"] == "ST10000VN000"
    assert 40 <= disk["util"] <= 60 and disk["read"] > 0 and disk["write"] > 0
    assert len(s["history"]) == 1 and s["host_view"]  # pierwsza próbka nie ma poprzedniej
    assert sysinfo.fmt_rate(6.2 * 1024) == "6.2 KB/s" and sysinfo.fmt_uptime(200000) == "2 dni 7 godz."


def test_missing_sensors_do_not_break(tmp_path):
    nas = sysinfo.SysInfo(proc=tmp_path / "brak", sys_dir=tmp_path / "brak-sys", host_proc=tmp_path / "x")
    s = nas.snapshot()
    assert s["memory"]["total"] == 0 and s["temps"] == [] and s["disks"] == [] and s["cpu_temp"] is None


def test_system_page(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    proc, sys_dir, host = fake_nas(tmp_path / "nas")
    real_init = sysinfo.SysInfo.__init__
    monkeypatch.setattr(sysinfo.SysInfo, "__init__",
                        lambda self, **kw: real_init(self, proc=proc, sys_dir=sys_dir, host_proc=host))
    monkeypatch.setattr(sysinfo.SysInfo, "start", lambda self: None)
    app = create_app(Settings(data_dir=tmp_path / "d", secret_key="x" * 40, cookie_secure=False,
                              admin_username="admin", admin_password="AdminHaslo123"))
    c = TestClient(app)
    import re
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"username": "admin", "password": "AdminHaslo123", "csrf": tok})
    page = c.get("/admin/system").text
    assert "Stan NAS" in page and "64.0 GB" in page and "41 °C" in page and "LAN 1" in page
    assert "ST10000VN000" in page and "418 obr./min" in page and "Pentium" in page
    live = c.get("/admin/system/live").text
    assert "<html" not in live and "Struktura pamięci" in live
    anon = TestClient(app, follow_redirects=False)
    assert anon.get("/admin/system/live").status_code == 303
