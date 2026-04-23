"""Isolated Alt+F test to verify the File shortcut in a fresh session."""

import os
import sys
import time
import pexpect

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "breakpoint_hook_demo.py")
VENV_PY = os.path.join(os.path.dirname(HERE), ".venv", "bin", "python")


def main():
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(HERE), "src")}
    child = pexpect.spawn(
        VENV_PY, [DEMO],
        env=env, encoding="utf-8", timeout=30,
        dimensions=(50, 160),
    )
    log = open("/tmp/menu_f_only_test.log", "w")
    child.logfile = log
    try:
        child.expect("Paused", timeout=20)
        time.sleep(0.3)

        print("Sending Alt+F (no prior menu interactions)...")
        child.send("\x1bf")  # Alt+F
        try:
            child.expect("Open file to debug", timeout=8)
            print("PASS: File modal opened")
            return 0
        except pexpect.exceptions.TIMEOUT:
            print("FAIL: File modal did not open")
            return 1
    finally:
        log.close()
        try:
            child.close(force=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
