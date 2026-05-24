"""Verify tdb reaches 'Paused' when debuggee runs under a --python venv
that doesn't have debugpy installed (adapter should still use tdb's Python)."""

import os
import sys
import pexpect

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(os.path.dirname(HERE), ".venv", "bin", "python")
ALT_PY = "/home/al/venvs/pygame/bin/python"
PROGRAM = "/tmp/pyblasteroids/pyblasteroids.py"


def main():
    if not os.path.isfile(ALT_PY):
        print(f"SKIP: {ALT_PY} not present")
        return 0
    if not os.path.isfile(PROGRAM):
        print(f"SKIP: {PROGRAM} not present")
        return 0

    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(HERE), "src")}
    child = pexpect.spawn(
        VENV_PY,
        ["-m", "tdb", "--python", ALT_PY, PROGRAM],
        env=env,
        encoding="utf-8",
        timeout=30,
        dimensions=(40, 140),
    )
    log = open("/tmp/alt_python_test.log", "w")
    child.logfile = log
    try:
        print("Waiting for tdb to reach Paused under alt Python...")
        child.expect("Paused", timeout=25)
        print("  Paused — adapter launched OK, debuggee ran under alt Python.")
        child.sendcontrol("q")
        try:
            child.expect(pexpect.EOF, timeout=10)
        except pexpect.exceptions.TIMEOUT:
            pass
        child.close(force=True)
        print("PASS")
        return 0
    except (pexpect.exceptions.TIMEOUT, pexpect.exceptions.EOF) as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        print("--- last output ---")
        print((child.before or "")[-300:])
        return 1
    finally:
        log.close()
        try:
            child.close(force=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
