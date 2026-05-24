"""End-to-end test for the two-step `q` → confirm → `q` quit flow."""

import os
import sys
import pexpect

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "breakpoint_hook_demo.py")
VENV_PY = os.path.join(os.path.dirname(HERE), ".venv", "bin", "python")


def main():
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(HERE), "src")}
    child = pexpect.spawn(
        VENV_PY,
        [DEMO],
        env=env,
        encoding="utf-8",
        timeout=30,
        dimensions=(40, 120),
    )
    log = open("/tmp/quit_confirm_test.log", "w")
    child.logfile = log
    try:
        print("Waiting for tdb to reach Paused...")
        child.expect("Paused", timeout=20)
        print("  Paused.")

        print("Sending first 'q' (should open confirm modal)...")
        child.send("q")
        child.expect("Hit q again to quit", timeout=10)
        print("  Confirm modal appeared.")

        print("Sending second 'q' (should quit tdb)...")
        child.send("q")

        print("Waiting for program to resume and print result...")
        child.expect("result = 45", timeout=15)
        print("  Got expected result.")

        child.expect(pexpect.EOF, timeout=10)
        child.close()
        print(f"Demo exit status: {child.exitstatus}")
        print("PASS" if child.exitstatus == 0 else "FAIL")
        return 0 if child.exitstatus == 0 else 1
    except (pexpect.exceptions.TIMEOUT, pexpect.exceptions.EOF) as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        print("--- captured output (last 500 chars) ---")
        print((child.before or "")[-500:])
        return 2
    finally:
        log.close()
        try:
            child.close(force=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
