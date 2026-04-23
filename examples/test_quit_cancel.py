"""Verify the quit-confirm modal cancels (via Escape) without quitting."""

import os
import sys
import pexpect

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "breakpoint_hook_demo.py")
VENV_PY = os.path.join(os.path.dirname(HERE), ".venv", "bin", "python")


def main():
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(HERE), "src")}
    child = pexpect.spawn(
        VENV_PY, [DEMO],
        env=env, encoding="utf-8", timeout=30,
        dimensions=(40, 120),
    )
    log = open("/tmp/quit_cancel_test.log", "w")
    child.logfile = log
    try:
        child.expect("Paused", timeout=20)

        print("Sending 'q' to open confirm modal...")
        child.send("q")
        child.expect("Hit q again to quit", timeout=10)
        print("  Modal appeared.")

        print("Sending Escape to cancel...")
        child.send("\x1b")  # ESC

        # Give the modal a moment to dismiss, then confirm tdb is still alive
        # by sending Ctrl+Q (immediate quit) and checking the program resumes.
        import time
        time.sleep(0.5)
        print("Sending Ctrl+Q to cleanly quit tdb (should work since cancel didn't quit)...")
        child.sendcontrol("q")

        child.expect("result = 45", timeout=15)
        print("  Got expected result after Ctrl+Q.")

        child.expect(pexpect.EOF, timeout=10)
        child.close()
        print(f"Demo exit status: {child.exitstatus}")
        print("PASS" if child.exitstatus == 0 else "FAIL")
        return 0 if child.exitstatus == 0 else 1
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
