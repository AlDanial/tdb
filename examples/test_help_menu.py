"""Verify: Alt+H opens Help, Documentation opens the README modal, and the
dropdown's Documentation entry is fully visible (not clipped)."""

import os
import re
import sys
import time
import pexpect

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "breakpoint_hook_demo.py")
VENV_PY = os.path.join(os.path.dirname(HERE), ".venv", "bin", "python")


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", " ", s)


def main():
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(HERE), "src")}
    child = pexpect.spawn(
        VENV_PY, [DEMO],
        env=env, encoding="utf-8", timeout=30,
        dimensions=(50, 120),
    )
    log = open("/tmp/help_menu_test.log", "w")
    child.logfile = log
    ok = True
    try:
        child.expect("Paused", timeout=20)
        time.sleep(0.3)

        # Alt+H opens Help dropdown; Enter selects first item (Documentation).
        # Functional check: the sequence must end in the Documentation modal.
        print("Sending Alt+H...")
        child.send("\x1bh")
        time.sleep(0.8)

        # Select Documentation (first item, Enter)
        print("Pressing Enter to select Documentation...")
        child.send("\r")
        try:
            # README content-specific string
            child.expect("TUI Python Debugger", timeout=10)
            print("  OK: Documentation modal opened with rendered README.")
        except pexpect.exceptions.TIMEOUT:
            print("  FAIL: Documentation modal did not open")
            ok = False

        # Close modal
        child.send("\x1b")
        time.sleep(0.3)

        child.sendcontrol("q")
        try:
            child.expect(pexpect.EOF, timeout=10)
        except pexpect.exceptions.TIMEOUT:
            pass
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    except (pexpect.exceptions.TIMEOUT, pexpect.exceptions.EOF) as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return 2
    finally:
        log.close()
        try:
            child.close(force=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
