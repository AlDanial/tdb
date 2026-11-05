"""Runs `debugpy --pid`, moving the injected code onto its own thread.

gdb stays inside the target for as long as the injected call lasts. debugpy's
code spends that time importing, and `import debugpy` runs `uname` through
`platform.platform()`. gdb leaves the other threads stopped after that vfork,
so a program with another thread wanting the GIL hangs for good. Here the
call only starts a thread and returns, so gdb is gone in moments and the slow
part runs with no debugger in the process.

Run as a script, with the marker path followed by debugpy's arguments, so
that it needs nothing from tdb. `background_code` is imported by tdb's tests.
"""

import os
import sys


# Far below the 5 ms default: with every thread busy, each file the imports
# touch otherwise costs a full turn around them, and attaching takes minutes.
ATTACH_SWITCH_INTERVAL = 0.0001


def as_bytes(text: str) -> str:
    """Python source for `text`, free of the quotes gdb can't carry."""
    return f"bytes({list(text.encode())})"


def background_code(code: str, ran_marker: str) -> str:
    """Python source that runs `code` on a new thread and returns.

    Creating `ran_marker` proves the code reached the target. It comes first
    because it fails if tdb gave up and removed the directory, and then
    nothing dials back. threading is imported here because debugpy's attach
    takes the importing thread for the main one.
    """
    body = (
        "import sys\n"
        "interval = sys.getswitchinterval()\n"
        f"sys.setswitchinterval({ATTACH_SWITCH_INTERVAL})\n"
        "try:\n"
        f"    exec({code!r}, {{}})\n"
        "finally:\n"
        "    sys.setswitchinterval(interval)\n"
    )
    return (
        "import _thread, threading;"
        f"open({as_bytes(ran_marker)}.decode(), chr(119)).close();"
        f"_thread.start_new_thread(exec, ({as_bytes(body)}, {{}}))"
    )


def main() -> None:
    ran_marker = sys.argv.pop(1)

    from debugpy.server import cli

    sys.path.append(
        os.path.join(os.path.dirname(cli.pydevd.__file__), "pydevd_attach_to_process")
    )
    import add_code_to_python_process as injector

    run_python_code = injector.run_python_code

    def run_on_a_thread(pid, python_code, *args, **kwargs):
        code = background_code(python_code, ran_marker)
        return run_python_code(pid, code, *args, **kwargs)

    injector.run_python_code = run_on_a_thread
    cli.main()


if __name__ == "__main__":
    main()
