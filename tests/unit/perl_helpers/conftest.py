"""Run Devel::TdbHelper functions under plain perl and capture the
marked JSON they emit. Skips the whole directory when perl >= 5.18 is
not available."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HELPERS = Path(__file__).resolve().parents[3] / "src/tdb/adapters/perl/helpers.pl"
MARK = re.compile(r"TDB>>>(.*?)<<<TDB", re.S)


def _perl_ok() -> bool:
    perl = shutil.which("perl")
    if perl is None:
        return False
    return subprocess.run([perl, "-e", "require v5.18"]).returncode == 0


pytestmark_skip = pytest.mark.skipif(not _perl_ok(), reason="perl >= 5.18 required")


@pytest.fixture
def run_helper():
    """run_helper(perl_code) -> list of decoded JSON payloads."""

    def _run(code: str) -> list[dict]:
        script = f"do {str(HELPERS)!r} or die $@ || $!;\n{code}\n"
        proc = subprocess.run(
            ["perl", "-e", script], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        return [json.loads(m) for m in MARK.findall(proc.stdout)]

    return _run
