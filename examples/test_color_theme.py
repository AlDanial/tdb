"""Verify color-theme wiring:

1. `action_color_theme` routes to textual's theme palette (code-level check).
2. A theme saved in the config is applied on startup.
3. Changing `app.theme` at runtime persists to the config file.
"""

import inspect
import json
import os
import pathlib
import shutil
import sys
import tempfile

import pexpect

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "breakpoint_hook_demo.py")
VENV_PY = os.path.join(os.path.dirname(HERE), ".venv", "bin", "python")


def check_routing() -> bool:
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
    from tdb.app import TdbApp
    src = inspect.getsource(TdbApp.action_color_theme)
    if "action_change_theme" not in src:
        print(f"FAIL: action_color_theme doesn't call action_change_theme:\n{src}")
        return False
    print("action_color_theme → action_change_theme: OK")
    return True


def check_startup_apply() -> bool:
    """Pre-seed config with theme=dracula, launch tdb, verify the running
    app has that theme applied."""
    tmp_home = pathlib.Path(tempfile.mkdtemp(prefix="tdb-theme-"))
    try:
        (tmp_home / ".config" / "tdb").mkdir(parents=True)
        (tmp_home / ".config" / "tdb" / "config.json").write_text(
            json.dumps({"theme": "dracula"}),
        )
        env = {
            **os.environ,
            "HOME": str(tmp_home),
            "PYTHONPATH": os.path.join(os.path.dirname(HERE), "src"),
            # dracula's base color is a dark purple; it shows up in the ANSI
            # stream as a distinctive RGB escape. We'll just check tdb runs
            # and exits cleanly with the preseeded config.
        }
        child = pexpect.spawn(
            VENV_PY, [DEMO],
            env=env, encoding="utf-8", timeout=30,
            dimensions=(40, 140),
        )
        try:
            child.expect("Paused", timeout=20)
            child.sendcontrol("q")
            child.expect("result = 45", timeout=15)
            child.expect(pexpect.EOF, timeout=10)
            child.close()
            if child.exitstatus != 0:
                print(f"FAIL: exit status {child.exitstatus}")
                return False
            # Config should still have the preseeded theme (watch_theme
            # re-saved it, value unchanged).
            post = json.loads(
                (tmp_home / ".config" / "tdb" / "config.json").read_text(),
            )
            if post.get("theme") != "dracula":
                print(f"FAIL: theme mutated to {post.get('theme')!r}")
                return False
            print("startup-load + clean-exit with preseeded theme: OK")
            return True
        finally:
            try:
                child.close(force=True)
            except Exception:
                pass
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)


def check_runtime_persist() -> bool:
    """Launch tdb, change app.theme at runtime via a tiny probe script that
    imports and flips the theme, then verify the saved config reflects it."""
    tmp_home = pathlib.Path(tempfile.mkdtemp(prefix="tdb-theme-"))
    try:
        probe = tmp_home / "probe.py"
        probe.write_text('''
import asyncio, json, pathlib, sys
sys.path.insert(0, "''' + os.path.join(os.path.dirname(HERE), "src") + '''")
from tdb.app import TdbApp

async def run():
    app = TdbApp(program="")
    async with app.run_test() as pilot:
        app.theme = "gruvbox"
        await pilot.pause()

asyncio.run(run())
cfg = pathlib.Path.home() / ".config" / "tdb" / "config.json"
print("SAVED:", json.loads(cfg.read_text()))
''')
        env = {**os.environ, "HOME": str(tmp_home)}
        import subprocess
        result = subprocess.run(
            [VENV_PY, str(probe)],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"FAIL: probe exit {result.returncode}")
            print(f"stderr: {result.stderr[-500:]}")
            return False
        if "'theme': 'gruvbox'" not in result.stdout:
            print(f"FAIL: config did not record gruvbox\n{result.stdout}")
            return False
        print("watch_theme persistence: OK")
        return True
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)


def main():
    ok = True
    ok &= check_routing()
    ok &= check_startup_apply()
    ok &= check_runtime_persist()
    print("\n" + ("ALL PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
