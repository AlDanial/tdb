"""A perl program that dies must not report exitCode 0."""

import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, "tests/integration")
from perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)


async def _run_to_exit(tmp_path, source):
    prog = tmp_path / "p.pl"
    prog.write_text(source)
    c = AdapterClient()
    await c.start()
    await c.request("initialize", {})
    c.send("launch", {"program": str(prog), "cwd": str(tmp_path)})
    await c.wait_event("initialized")
    await c.request("configurationDone", {})
    await c.wait_event("stopped")
    await c.request("continue", {"threadId": 1})
    ev = await c.wait_event("exited", timeout=30)
    await c.stop()
    return ev["body"]["exitCode"]


async def test_clean_exit_reports_zero(tmp_path):
    assert await _run_to_exit(tmp_path, 'print "ok\\n";\n') == 0


async def test_die_reports_nonzero(tmp_path):
    code = await _run_to_exit(tmp_path, "my $x = 0;\nmy $y = 1 / $x;\n")
    assert code != 0
