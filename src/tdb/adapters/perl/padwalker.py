"""Bundled PadWalker: locate, build on first use, cache per perl.

PadWalker (v2.5, Robin Houston) lets helpers.pl inspect lexicals in
outer/caller frames. Its `PadWalker.pm` is a thin wrapper over XS code,
so the pure-Perl file alone is useless: `bootstrap` needs a compiled
`auto/PadWalker/PadWalker.<dlext>` built against the exact perl that
runs the debuggee (version, thread model, architecture). A pure-Python
wheel cannot compile that at install time, and the right perl is only
known at launch time (`--perl` can pick any interpreter), so tdb builds
it lazily, once per interpreter, and caches the result:

    <config dir>/padwalker/<perl version>-<archname>-pw<PadWalker version>/
        PadWalker.pm
        auto/PadWalker/PadWalker.<dlext>

Resolution order at launch (`ensure_padwalker`):
  1. The target perl already loads PadWalker with the user's environment
     (CPAN or distro install) -> add nothing. Shadowing a working install
     with our .pm could pair a 2.5 .pm with an older .so and fail the
     XS version check, so we never override one that works.
  2. A cached build for this perl exists and loads -> use it.
  3. Build from the vendored sources (needs perl's CORE headers and a C
     compiler; ExtUtils::MakeMaker is core) -> cache, use it.
  4. Otherwise report why, and the adapter degrades to its core-B pad
     walk exactly as it did before PadWalker was bundled.

The cache root honours TDB_PADWALKER_CACHE (tests point it at a temp
dir so real builds never touch ~/.config/tdb) and tolerates a read-only
config dir by falling back to the system temp dir.
"""

from __future__ import annotations

import importlib.resources
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PADWALKER_VERSION = "2.5"
SOURCE_FILES = ("PadWalker.pm", "PadWalker.xs", "Makefile.PL")
BUILD_TIMEOUT = 300.0
PROBE_TIMEOUT = 30.0

# A perl one-liner that fails unless PadWalker both loads and bootstraps.
_PROBE = "require PadWalker; PadWalker::peek_my(0); print $INC{'PadWalker.pm'}"
_CONFIG = (
    "use Config; print join qq(\\n), "
    "$Config{version}, $Config{archname}, $Config{make}, $Config{dlext}"
)


@dataclass(frozen=True)
class PadWalkerResult:
    """Outcome of `ensure_padwalker`.

    lib_dir: directory to prepend to PERL5LIB, or None when nothing is
    needed (native install) or nothing is available (failure).
    source: "native" | "cached" | "built" | "unavailable".
    message: one user-facing line worth showing once, or None.
    """

    lib_dir: str | None
    source: str
    message: str | None = None


def padwalker_dir() -> str:
    """Directory holding the vendored PadWalker sources (.pm, .xs, Makefile.PL)."""
    return str(importlib.resources.files("tdb.adapters.perl") / "padwalker_src")


def with_perl5lib(env: dict, directory: str) -> dict:
    """Return a copy of `env` with `directory` at the front of PERL5LIB.

    Uses os.pathsep (":" on POSIX, ";" on Windows) -- perl splits PERL5LIB
    on the platform separator. An existing entry equal to `directory` is
    dropped so repeated launches (restart) don't accumulate duplicates.
    """
    out = dict(env)
    existing = [
        e for e in (out.get("PERL5LIB") or "").split(os.pathsep) if e and e != directory
    ]
    out["PERL5LIB"] = os.pathsep.join([directory, *existing])
    return out


def cache_root() -> Path:
    """Where per-perl PadWalker builds live. TDB_PADWALKER_CACHE overrides."""
    override = os.environ.get("TDB_PADWALKER_CACHE")
    if override:
        return Path(override)
    from tdb.persist import CONFIG_DIR

    return CONFIG_DIR / "padwalker"


def _run(argv: list[str], env: dict, cwd: str | None = None, timeout: float = PROBE_TIMEOUT):
    return subprocess.run(
        argv,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _loads(perl: str, env: dict) -> bool:
    try:
        return _run([perl, "-e", _PROBE], env).returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("PadWalker probe via %s failed: %s", perl, e)
        return False


def _perl_config(perl: str, env: dict) -> tuple[str, str, str, str] | None:
    """(version, archname, make, dlext) of `perl`, or None if it won't run."""
    try:
        proc = _run([perl, "-e", _CONFIG], env)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("perl -MConfig via %s failed: %s", perl, e)
        return None
    parts = proc.stdout.split("\n") if proc.returncode == 0 else []
    if len(parts) != 4 or not all(parts):
        return None
    return parts[0], parts[1], parts[2], parts[3]


def cache_key(version: str, archname: str) -> str:
    return f"{version}-{archname}-pw{PADWALKER_VERSION}"


def _cache_candidates() -> list[Path]:
    """Preferred cache root first, then a temp-dir fallback for read-only
    config dirs (see the read-only filesystem handling elsewhere in tdb)."""
    primary = cache_root()
    fallback = Path(tempfile.gettempdir()) / f"tdb-padwalker-{os.getuid() if hasattr(os, 'getuid') else 'user'}"
    return [primary] if primary == fallback else [primary, fallback]


def _build(perl: str, env: dict, make: str, dlext: str, dest: Path) -> str | None:
    """Compile the vendored sources with `perl`; install the two artefacts
    into `dest`. Returns None on success, else a short reason."""
    src = Path(padwalker_dir())
    with tempfile.TemporaryDirectory(prefix="tdb-padwalker-build-") as tmp:
        for name in SOURCE_FILES:
            try:
                shutil.copy(src / name, tmp)
            except OSError as e:
                return f"bundled source {name} is missing ({e})"
        build_env = dict(env)
        # MakeMaker consults PERL5LIB for prerequisites only; a stale
        # entry pointing at another PadWalker must not confuse VERSION_FROM.
        build_env.pop("PERL5LIB", None)
        for argv in ([perl, "Makefile.PL"], [make]):
            try:
                proc = _run(argv, build_env, cwd=tmp, timeout=BUILD_TIMEOUT)
            except (OSError, subprocess.SubprocessError) as e:
                return f"could not run {argv[0]} ({e})"
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
                log.warning(
                    "PadWalker build step %s failed (rc=%s):\n%s",
                    argv,
                    proc.returncode,
                    proc.stderr or proc.stdout,
                )
                return f"`{' '.join(argv)}` failed: " + " | ".join(tail)
        pm = Path(tmp) / "blib" / "lib" / "PadWalker.pm"
        so = Path(tmp) / "blib" / "arch" / "auto" / "PadWalker" / f"PadWalker.{dlext}"
        if not (pm.is_file() and so.is_file()):
            return "build produced no PadWalker module (blib layout unexpected)"
        staging = dest.with_name(dest.name + f".tmp-{os.getpid()}")
        try:
            shutil.rmtree(staging, ignore_errors=True)
            (staging / "auto" / "PadWalker").mkdir(parents=True)
            shutil.copy(pm, staging / "PadWalker.pm")
            shutil.copy(so, staging / "auto" / "PadWalker" / so.name)
            shutil.rmtree(dest, ignore_errors=True)
            os.replace(staging, dest)
        except OSError as e:
            shutil.rmtree(staging, ignore_errors=True)
            return f"could not write cache dir {dest} ({e})"
    return None


_INSTALL_HINT = (
    "Lexicals in outer frames will be limited. Either install PadWalker "
    "(`cpanm PadWalker`, or a distro package such as libpadwalker-perl / "
    "perl-PadWalker), or install perl's headers (libperl-dev / perl-devel) "
    "and a C compiler so tdb can build its bundled copy."
)

_memo: dict[tuple[str, str], PadWalkerResult] = {}


def ensure_padwalker(perl: str, env: dict | None = None) -> PadWalkerResult:
    """Make PadWalker loadable by `perl`, building and caching if needed.

    Memoised per (perl, PERL5LIB) for the life of the process so a
    restart doesn't re-probe. Never raises.
    """
    env = dict(env if env is not None else os.environ)
    key = (perl, env.get("PERL5LIB", ""))
    if key in _memo:
        return _memo[key]
    result = _resolve(perl, env)
    _memo[key] = result
    if result.source != "native":
        log.info("PadWalker for %s: %s (%s)", perl, result.source, result.lib_dir)
    return result


def _resolve(perl: str, env: dict) -> PadWalkerResult:
    if _loads(perl, env):
        return PadWalkerResult(None, "native")
    cfg = _perl_config(perl, env)
    if cfg is None:
        return PadWalkerResult(
            None, "unavailable", f"tdb: PadWalker unavailable: cannot run {perl}. {_INSTALL_HINT}"
        )
    version, archname, make, dlext = cfg
    name = cache_key(version, archname)
    for root in _cache_candidates():
        cached = root / name
        if cached.is_dir() and _loads(perl, with_perl5lib(env, str(cached))):
            return PadWalkerResult(str(cached), "cached")
    reasons: list[str] = []
    for root in _cache_candidates():
        dest = root / name
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            reasons.append(f"cannot create {root} ({e})")
            continue
        reason = _build(perl, env, make, dlext, dest)
        if reason is None:
            if _loads(perl, with_perl5lib(env, str(dest))):
                return PadWalkerResult(
                    str(dest),
                    "built",
                    f"tdb: built the bundled PadWalker module for perl {version} "
                    f"({archname}); cached in {dest}",
                )
            reason = "built module failed to load"
        reasons.append(reason)
        # A compile failure won't get better in another cache root.
        if not reason.startswith("cannot create") and "cache dir" not in reason:
            break
    return PadWalkerResult(
        None,
        "unavailable",
        f"tdb: PadWalker unavailable for perl {version} ({archname}): "
        + "; ".join(reasons)
        + ". "
        + _INSTALL_HINT,
    )


def padwalker_status(perl: str = "perl") -> str:
    """One-line, side-effect-free status for `tdb --info`: never builds."""
    env = dict(os.environ)
    if shutil.which(perl) is None and not os.path.isfile(perl):
        return f"perl not found on PATH ({perl})"
    if _loads(perl, env):
        return f"available natively to {perl}"
    cfg = _perl_config(perl, env)
    if cfg is None:
        return f"{perl} does not run"
    version, archname, _make, _dlext = cfg
    name = cache_key(version, archname)
    for root in _cache_candidates():
        cached = root / name
        if cached.is_dir() and _loads(perl, with_perl5lib(env, str(cached))):
            return f"built for perl {version} ({archname}), cached in {cached}"
    return (
        f"not yet built for perl {version} ({archname}); tdb will try to "
        f"compile it into {cache_root() / name} on the first Perl launch"
    )
