"""End-to-end test for tdb.breakpoint() using pexpect."""

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
    log = open("/tmp/breakpoint_test.log", "w")
    child.logfile = log
    try:
        # Wait for the tdb TUI to render — look for the breakpoint_hook_demo filename
        # which should appear in the code view title bar.
        print("Waiting for tdb TUI to render...")
        child.expect("breakpoint_hook_demo", timeout=20)
        print("  Got tdb TUI.")

        # Send Ctrl+Q to quit tdb cleanly (remote-attach mode should detach
        # without terminating the user program).
        print("Sending Ctrl+Q to quit tdb...")
        child.sendcontrol("q")

        # After tdb exits, the user program should resume past
        # `debugpy.breakpoint()` and print `result = 45`.
        print("Waiting for result...")
        child.expect("result = 45", timeout=15)
        print("  Got expected result.")

        child.expect(pexpect.EOF, timeout=10)
        child.close()
        rc = child.exitstatus
        print(f"Demo exit status: {rc}")
        if rc != 0:
            print("FAIL: non-zero exit")
            return 1
        print("PASS")
        return 0
    except pexpect.exceptions.TIMEOUT as e:
        print(f"TIMEOUT: {e}")
        print("--- captured output ---")
        print(child.before)
        return 2
    except pexpect.exceptions.EOF as e:
        print(f"EOF: {e}")
        print("--- captured output ---")
        print(child.before)
        return 3
    finally:
        log.close()
        try:
            child.close(force=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
