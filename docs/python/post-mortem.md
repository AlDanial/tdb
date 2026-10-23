# Post-Mortem Exception Hook

You can have `tdb` pop open automatically when *any* Python program crashes without the
need to launch through `tdb` up front. Install the hook once at the top of your program:

```python
import sys
import tdb

sys.excepthook = tdb.exception_hook
```

When an uncaught exception reaches the hook, `tdb`:

1. Prints the standard Python traceback to stderr (so your scrollback still has a record)
2. Snapshots every frame in the traceback. This includes locals, plus one level of
recursion into containers (`dict`, `list`, `tuple`, `set`) and objects with `__dict__`
3. Launches the TUI in **post-mortem mode**, inheriting the current terminal

In post-mortem mode you can:

- Navigate the call stack (`u` / `d` or the Stack View) and see each frame's locals
- Expand nested containers and object attributes in the Variables View
- Read the full traceback (including chained `cause`/`context` exceptions) in the Console View
- Jump around the source with the full Code View (syntax highlighting, goto-line, etc.)

Stepping, continue, breakpoints, restart, and the Evaluate View are disabled. The original
interpreter is gone since the view is a frozen snapshot. Press `q` to exit.

The hook is a no-op when stdin/stdout aren't a tty (e.g. when your program is piped or
run from cron), so it is safe to leave installed in production code. Snapshots are
written to a temp file that is deleted as soon as `tdb` exits.

Snapshot depth / breadth is capped (5 levels, 50 children per container) to keep the
capture cheap even for pathological object graphs; cycles are handled via identity
memoization.

## Post-Mortem within a Docker Container

The `textual-debugger` GitHub repository's `examples/` directory has three files
that show how to run a `tdb`-enabled Python program under `tmux` in a Docker
container so that you can attach to the container and inspect the program
in `tdb` post-mortem analysis mode if the program hits an unhandled exception:

- [post_mortem_example.py](https://github.com/AlDanial/tdb/blob/main/examples/post_mortem_example.py)
- [post_mortem_entrypoint.sh](https://github.com/AlDanial/tdb/blob/main/examples/post_mortem_entrypoint.sh)
- [Dockerfile.post_mortem](https://github.com/AlDanial/tdb/blob/main/examples/Dockerfile.post_mortem)
