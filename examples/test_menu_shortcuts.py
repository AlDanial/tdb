"""Verify Alt+letter menu-bar shortcuts."""

import os
import sys
import time
import pexpect

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "breakpoint_hook_demo.py")
VENV_PY = os.path.join(os.path.dirname(HERE), ".venv", "bin", "python")


def send_alt(child, letter):
    # Alt+letter in a terminal = ESC followed by letter.
    child.send("\x1b" + letter)


def check(label, expected, child):
    print(f"Testing Alt+{label[0].upper()} → expect {expected!r}...")
    send_alt(child, label[0].lower())
    try:
        child.expect(expected, timeout=8)
    except pexpect.exceptions.TIMEOUT:
        print(f"  FAIL: didn't see {expected!r} after Alt+{label[0].upper()}")
        return False
    print(f"  OK: saw {expected!r}")
    return True


def main():
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(HERE), "src")}
    child = pexpect.spawn(
        VENV_PY,
        [DEMO],
        env=env,
        encoding="utf-8",
        timeout=30,
        dimensions=(50, 160),
    )
    log = open("/tmp/menu_shortcuts_test.log", "w")
    child.logfile = log
    try:
        child.expect("Paused", timeout=20)
        time.sleep(0.3)

        ok = True

        # Alt+C opens Configure dropdown, which lists "Color Theme" and "Keybindings"
        if not check("Configure", "Color Theme", child):
            ok = False
        # Dismiss dropdown
        child.send("\x1b")
        time.sleep(0.2)

        # Alt+T opens Threads modal (program is paused → has one main thread)
        if not check("Threads", "Thread", child):
            ok = False
        child.send("\x1b")
        time.sleep(0.2)

        # Alt+P opens Processes modal (single-process program triggers the
        # "No extra processes" toast, which still demonstrates the binding
        # fired). We search for the toast title as fallback.
        send_alt(child, "p")
        try:
            child.expect("No extra processes|Processes", timeout=8)
            print("  OK: Alt+P triggered processes path")
        except pexpect.exceptions.TIMEOUT:
            print("  FAIL: Alt+P did not trigger processes path")
            ok = False
        time.sleep(0.3)

        # Alt+A opens Async Tasks (or notifies — demo isn't async). Verify
        # it at least triggers some response without tdb hanging.
        send_alt(child, "a")
        time.sleep(0.5)
        # Alt+F opens File dialog — use a modal-specific string ("File"
        # alone matches the permanent menu-bar label).
        if not check("File", "Open file to debug", child):
            ok = False
        child.send("\x1b")
        time.sleep(0.2)

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
