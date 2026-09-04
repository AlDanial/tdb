from tdb.languages import registry


def test_ps1_extension_detects_powershell(tmp_path):
    p = tmp_path / "x.ps1"
    p.write_text("Write-Host 1\n")
    assert registry.detect(str(p)) == "powershell"


def test_psm1_extension_detects_powershell(tmp_path):
    p = tmp_path / "m.psm1"
    p.write_text("function F {}\n")
    assert registry.detect(str(p)) == "powershell"


def test_pwsh_shebang_detects_powershell(tmp_path):
    p = tmp_path / "script"
    p.write_text("#!/usr/bin/env pwsh\nWrite-Host 1\n")
    assert registry.detect(str(p)) == "powershell"


def test_extensions_for_powershell():
    assert registry.extensions_for("powershell") == (".ps1", ".psm1")


def test_resolve_default_adapter():
    assert registry.resolve("powershell").adapter.id == "pses"
