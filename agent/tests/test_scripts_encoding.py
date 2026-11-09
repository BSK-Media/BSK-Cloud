from pathlib import Path


def test_powershell_scripts_have_utf8_bom():
    """Windows PowerShell 5.1 czyta .ps1 bez BOM jako ANSI — polskie znaki psują wtedy składnię skryptu."""
    for name in ("install-windows.ps1", "uninstall-windows.ps1"):
        data = (Path(__file__).resolve().parent.parent / name).read_bytes()
        assert data.startswith(b"\xef\xbb\xbf"), name
        data.decode("utf-8")
