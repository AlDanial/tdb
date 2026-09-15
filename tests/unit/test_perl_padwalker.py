"""Unit tests for the bundled-PadWalker resolver (perl stubbed out)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tdb.adapters.perl import padwalker as pw


@pytest.fixture(autouse=True)
def _clear_memo():
    pw._memo.clear()
    yield
    pw._memo.clear()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    monkeypatch.setenv("TDB_PADWALKER_CACHE", str(root))
    # Keep the temp-dir fallback inside tmp_path too.
    monkeypatch.setattr(pw.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return root


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


CFG = "5.40.1\nx86_64-linux-gnu-thread-multi\nmake\nso"


def _fake_perl(monkeypatch, *, native: bool, cached_ok: bool, build_ok: bool = True):
    """Simulate perl: the probe succeeds only when PERL5LIB points at a
    directory holding PadWalker.pm (or natively); Makefile.PL/make write
    a fake blib tree."""
    calls: list[list[str]] = []

    def run(argv, env=None, cwd=None, **kw):
        calls.append(list(argv))
        if argv[0] == "make":
            if not build_ok:
                return _Proc(2, "", "gcc: error: perl.h: No such file")
            (Path(cwd) / "blib" / "lib").mkdir(parents=True)
            (Path(cwd) / "blib" / "lib" / "PadWalker.pm").write_text("pm")
            d = Path(cwd) / "blib" / "arch" / "auto" / "PadWalker"
            d.mkdir(parents=True)
            (d / "PadWalker.so").write_bytes(b"\x7fELF")
            return _Proc(0)
        if argv[-1] == "Makefile.PL":
            return _Proc(0)
        script = argv[-1]
        if script == pw._CONFIG:
            return _Proc(0, CFG)
        if script == pw._PROBE:
            if native:
                return _Proc(0, "/usr/lib/perl5/PadWalker.pm")
            first = (env or {}).get("PERL5LIB", "").split(os.pathsep)[0]
            if first and (Path(first) / "PadWalker.pm").is_file() and (cached_ok or "tmp-" not in first):
                return _Proc(0, first)
            return _Proc(2, "", "Can't locate PadWalker.pm")
        raise AssertionError(f"unexpected perl call {argv}")

    monkeypatch.setattr(pw.subprocess, "run", run)
    return calls


def test_native_install_adds_nothing(monkeypatch, cache):
    calls = _fake_perl(monkeypatch, native=True, cached_ok=True)
    r = pw.ensure_padwalker("perl", {})
    assert r == pw.PadWalkerResult(None, "native")
    assert len(calls) == 1  # one probe, no config, no build
    assert not cache.exists()


def test_builds_and_caches_then_reuses(monkeypatch, cache):
    calls = _fake_perl(monkeypatch, native=False, cached_ok=True)
    r = pw.ensure_padwalker("perl", {"PERL5LIB": "/user"})
    expected = cache / pw.cache_key("5.40.1", "x86_64-linux-gnu-thread-multi")
    assert r.source == "built"
    assert r.lib_dir == str(expected)
    assert (expected / "PadWalker.pm").is_file()
    assert (expected / "auto" / "PadWalker" / "PadWalker.so").is_file()
    assert "built the bundled PadWalker" in r.message
    assert not list(cache.glob("*.tmp-*")), "staging dir must be renamed away"
    assert any(a[0] == "make" for a in calls)
    make_env_calls = [a for a in calls if a[-1] == "Makefile.PL"]
    assert make_env_calls

    # Second process (memo cleared) -> cached, no build.
    pw._memo.clear()
    calls.clear()
    r2 = pw.ensure_padwalker("perl", {"PERL5LIB": "/user"})
    assert r2 == pw.PadWalkerResult(str(expected), "cached")
    assert not any(a[0] == "make" for a in calls)


def test_memoised_per_perl_and_perl5lib(monkeypatch, cache):
    calls = _fake_perl(monkeypatch, native=True, cached_ok=True)
    pw.ensure_padwalker("perl", {})
    pw.ensure_padwalker("perl", {})
    assert len(calls) == 1
    pw.ensure_padwalker("/opt/perl", {})
    assert len(calls) == 2


def test_build_failure_reports_reason_and_hint(monkeypatch, cache):
    _fake_perl(monkeypatch, native=False, cached_ok=True, build_ok=False)
    r = pw.ensure_padwalker("perl", {})
    assert r.lib_dir is None and r.source == "unavailable"
    assert "perl.h" in r.message
    assert "libperl-dev" in r.message
    assert not any(cache.glob("*")) if cache.exists() else True


def test_perl_missing_reports_unavailable(monkeypatch, cache):
    def run(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(pw.subprocess, "run", run)
    r = pw.ensure_padwalker("/nonexistent/perl", {})
    assert r.source == "unavailable"
    assert "cannot run /nonexistent/perl" in r.message


def test_read_only_config_dir_falls_back_to_tempdir(monkeypatch, cache, tmp_path):
    _fake_perl(monkeypatch, native=False, cached_ok=True)
    real_mkdir = Path.mkdir

    def mkdir(self, *a, **kw):
        if str(self).startswith(str(cache)):
            raise OSError(30, "Read-only file system")
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    r = pw.ensure_padwalker("perl", {})
    assert r.source == "built"
    assert r.lib_dir.startswith(str(tmp_path / "tmp"))


def test_status_never_builds(monkeypatch, cache):
    calls = _fake_perl(monkeypatch, native=False, cached_ok=True)
    monkeypatch.setattr(pw.shutil, "which", lambda p: "/usr/bin/perl")
    text = pw.padwalker_status("perl")
    assert "not yet built" in text
    assert not any(a[0] == "make" for a in calls)
    assert not cache.exists()


def test_with_perl5lib_uses_platform_separator():
    env = {"PERL5LIB": os.pathsep.join(["/a", "/x"])}
    assert pw.with_perl5lib(env, "/x")["PERL5LIB"] == os.pathsep.join(["/x", "/a"])


def test_subprocess_timeout_is_a_soft_failure(monkeypatch, cache):
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(pw.subprocess, "run", run)
    assert pw.ensure_padwalker("perl", {}).source == "unavailable"
