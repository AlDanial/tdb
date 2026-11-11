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
            if (
                first
                and (Path(first) / "PadWalker.pm").is_file()
                and (cached_ok or "tmp-" not in first)
            ):
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


# --- ExtUtils::MakeMaker ---------------------------------------------------------


def _fake_makemaker(monkeypatch, rc, out="", err=""):
    calls = []

    def run(argv, env=None, cwd=None, **kw):
        calls.append(list(argv))
        return _Proc(rc, out, err)

    monkeypatch.setattr(pw.subprocess, "run", run)
    return calls


def test_makemaker_version_reports_version_string(monkeypatch):
    calls = _fake_makemaker(monkeypatch, 0, "7.70")
    assert pw.makemaker_version("perl") == "7.70"
    assert calls == [["perl", "-MExtUtils::MakeMaker", "-e", pw._MAKEMAKER_PROBE]]


def test_makemaker_version_none_when_module_missing(monkeypatch):
    _fake_makemaker(
        monkeypatch,
        2,
        "",
        "Can't locate ExtUtils/MakeMaker.pm in @INC (you may need to install "
        "the ExtUtils::MakeMaker module)\nBEGIN failed--compilation aborted.\n",
    )
    assert pw.makemaker_version("perl") is None


def test_makemaker_version_none_when_perl_does_not_run(monkeypatch):
    def run(argv, **kw):
        raise OSError("no such perl")

    monkeypatch.setattr(pw.subprocess, "run", run)
    assert pw.makemaker_version("/nope/perl") is None


def test_build_without_makemaker_degrades_with_a_pointed_hint(monkeypatch, cache):
    """`perl Makefile.PL` dying on a missing ExtUtils::MakeMaker is the
    common failure on minimal distro perls: the resolver must come back
    unavailable (so the launch proceeds without PadWalker) and name the
    module to install."""
    calls = []

    def run(argv, env=None, cwd=None, **kw):
        calls.append(list(argv))
        if argv[-1] == "Makefile.PL":
            return _Proc(
                2,
                "",
                "Can't locate ExtUtils/MakeMaker.pm in @INC (you may need to "
                "install the ExtUtils::MakeMaker module) (@INC entries checked: "
                "/usr/share/perl5).\nBEGIN failed--compilation aborted at "
                "Makefile.PL line 1.\n",
            )
        if argv[0] == "make":
            raise AssertionError("make must not run when Makefile.PL failed")
        script = argv[-1]
        if script == pw._CONFIG:
            return _Proc(0, CFG)
        if script == pw._PROBE:
            return _Proc(2, "", "Can't locate PadWalker.pm")
        raise AssertionError(f"unexpected perl call {argv}")

    monkeypatch.setattr(pw.subprocess, "run", run)
    r = pw.ensure_padwalker("perl", {})
    assert r.source == "unavailable"
    assert r.lib_dir is None
    assert "ExtUtils::MakeMaker" in r.message
    assert "variable inspection" in r.message.lower() or "Lexicals" in r.message
    assert ["make"] not in calls


def test_is_loadable_native(monkeypatch, cache):
    _fake_perl(monkeypatch, native=True, cached_ok=True)
    assert pw.is_loadable("perl") is True


def test_is_loadable_cached_build(monkeypatch, cache):
    _fake_perl(monkeypatch, native=False, cached_ok=True)
    d = cache / pw.cache_key("5.40.1", "x86_64-linux-gnu-thread-multi")
    d.mkdir(parents=True)
    (d / "PadWalker.pm").write_text("pm")
    assert pw.is_loadable("perl") is True


def test_is_loadable_false_without_native_or_cache(monkeypatch, cache):
    _fake_perl(monkeypatch, native=False, cached_ok=True)
    assert pw.is_loadable("perl") is False


# --- shared temp-dir fallback must be private (local code execution) -----

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX ownership/permissions")


def _plant(root: Path) -> Path:
    """What another local user could pre-create in the shared temp dir."""
    d = root / pw.cache_key("5.40.1", "x86_64-linux-gnu-thread-multi")
    d.mkdir(parents=True)
    (d / "PadWalker.pm").write_text("system('evil')")
    return d


def _fallback_root(tmp_path: Path) -> Path:
    return pw._cache_candidates()[-1]


@posix_only
def test_world_writable_fallback_cache_is_not_loaded(monkeypatch, cache, tmp_path):
    _fake_perl(monkeypatch, native=False, cached_ok=True)
    root = _fallback_root(tmp_path)
    planted = _plant(root)
    root.chmod(0o777)
    r = pw.ensure_padwalker("perl", {})
    assert r.lib_dir != str(planted)
    assert r.source == "built" and r.lib_dir.startswith(str(cache))


@posix_only
def test_foreign_owned_fallback_cache_is_not_loaded(monkeypatch, cache, tmp_path):
    _fake_perl(monkeypatch, native=False, cached_ok=True)
    root = _fallback_root(tmp_path)
    planted = _plant(root)
    root.chmod(0o700)
    real_lstat = os.lstat

    class _Foreign:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            if name == "st_uid":
                return os.geteuid() + 1
            return getattr(self._st, name)

    def lstat(p, *a, **kw):
        st = real_lstat(p, *a, **kw)
        return _Foreign(st) if Path(p) == root else st

    monkeypatch.setattr(pw.os, "lstat", lstat)
    for probe in (pw.ensure_padwalker, pw.is_loadable):
        pw._memo.clear()
        r = probe("perl", {})
        lib = getattr(r, "lib_dir", None)
        assert lib != str(planted)


@posix_only
def test_symlinked_fallback_cache_is_not_loaded(monkeypatch, cache, tmp_path):
    _fake_perl(monkeypatch, native=False, cached_ok=True)
    elsewhere = tmp_path / "attacker"
    _plant(elsewhere)
    root = _fallback_root(tmp_path)
    root.symlink_to(elsewhere, target_is_directory=True)
    assert pw.is_loadable("perl") is False


@posix_only
def test_fallback_root_created_private(monkeypatch, cache, tmp_path):
    _fake_perl(monkeypatch, native=False, cached_ok=True)
    real_mkdir = Path.mkdir

    def mkdir(self, *a, **kw):
        if str(self).startswith(str(cache)):
            raise OSError(30, "Read-only file system")
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    old = os.umask(0o022)
    try:
        r = pw.ensure_padwalker("perl", {})
    finally:
        os.umask(old)
    assert r.source == "built"
    assert (os.stat(_fallback_root(tmp_path)).st_mode & 0o077) == 0


def test_cache_root_follows_config_dir(monkeypatch, tmp_path):
    from tdb.adapters.perl import padwalker as pw

    monkeypatch.delenv("TDB_PADWALKER_CACHE", raising=False)
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path))
    assert pw.cache_root() == tmp_path / "padwalker"
