"""Real build of the vendored PadWalker against the local perl.

Skips when perl or its XS toolchain (CORE headers + C compiler) is
absent; passes when tdb can compile, cache and load the module."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tdb.adapters.perl import padwalker as pw

pytestmark = pytest.mark.skipif(shutil.which("perl") is None, reason="perl not installed")


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    pw._memo.clear()
    monkeypatch.setenv("TDB_PADWALKER_CACHE", str(tmp_path / "cache"))
    yield
    pw._memo.clear()


def _toolchain_ok() -> bool:
    cfg = pw._perl_config("perl", dict(os.environ))
    if cfg is None:
        return False
    hdr = subprocess.run(
        ["perl", "-MConfig", "-e", "print -f qq($Config{archlibexp}/CORE/perl.h) ? 1 : 0"],
        capture_output=True,
        text=True,
    ).stdout
    return hdr == "1" and shutil.which(cfg[2]) is not None


def test_real_build_loads_and_peeks_outer_lexicals(tmp_path):
    env = dict(os.environ)
    # Hide any native PadWalker so the build path is exercised.
    env.pop("PERL5LIB", None)
    if pw._loads("perl", env):
        pytest.skip("PadWalker already installed natively; build path not exercised")
    if not _toolchain_ok():
        pytest.skip("perl CORE headers or C compiler missing")

    r = pw.ensure_padwalker("perl", env)
    assert r.source == "built", r.message
    assert r.lib_dir and r.lib_dir.startswith(str(tmp_path / "cache"))

    script = (
        "use PadWalker qw(peek_my); sub f { my $h = peek_my(1); "
        "print join(',', sort keys %$h) } my $outer = 1; my @arr; f();"
    )
    proc = subprocess.run(
        ["perl", "-e", script],
        env=pw.with_perl5lib(env, r.lib_dir),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "$outer,@arr"

    pw._memo.clear()
    assert pw.ensure_padwalker("perl", env).source == "cached"
