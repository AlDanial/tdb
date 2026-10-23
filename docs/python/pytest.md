# pytest Integration (`--pdbcls`)

`tdb` can stand in for `pdb` when `pytest` stops on a failing test, the
same way IPython (`--pdbcls=IPython.terminal.debugger:Pdb`) and pudb
(`--pdbcls=pudb.debugger:Debugger`) do:

```bash
pytest --pdb --pdbcls=tdb.pytest_debugger:Debugger
```

`-s` / `--capture=no` is not required; pytest suspends output capturing
before handing control to the debugger. To make it the default, add the
options to your pytest configuration:

```toml
[tool.pytest.ini_options]
addopts = "--pdbcls=tdb.pytest_debugger:Debugger"
```

(`tdb.pytest_debugger:Pdb` is an alias for the same class.)

What you get depends on how pytest enters the debugger:

| pytest trigger | `tdb` behavior |
|---|---|
| `--pdb`, test fails or errors | Post-mortem TUI on the failing test, as with [`tdb.exception_hook`](post-mortem.md). Navigate the stack and inspect every frame's variables at the point of failure. pytest's own runner frames are trimmed from the Stack View. Quit `tdb` (`q`, then `q` again to confirm) and pytest moves on to the next test. |
| `breakpoint()` or `pytest.set_trace()` in a test | Live `tdb` attached to the pytest process, paused on the line after the call, as with [`tdb.breakpoint()`](breakpoint-hook.md). Step, evaluate, set breakpoints, continue. |
| `--trace` | Live `tdb` paused on the first line of each test. |

Notes:

- Post-mortem mode is a frozen snapshot: no Evaluate View, no stepping.
  For a live view of a failure, put a `breakpoint()` in the test (or use
  `--trace`) and `continue`. `tdb` stops on the failing line with the
  interpreter still alive.
- One live `tdb` serves the whole pytest run. After `continue` it stays
  attached, showing **Running**, and the next `breakpoint()` or `--trace`
  stop appears in it. If a test fails under `--pdb` while it is still
  attached, the post-mortem view opens once you quit the live `tdb`.
- Every failure opens its own post-mortem session. Combine with `-x` or
  `--maxfail=N` to limit how many you step through.
- Live stops need `debugpy` installed in the environment running pytest.
- When stdin/stdout aren't a terminal (CI, `pytest-xdist` workers), the
  class falls back to plain `pdb` behavior.
