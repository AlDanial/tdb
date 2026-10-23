# Recording and replaying sessions

`tdb --record session.jsonl prog.py` runs a normal TUI session and captures
your debugging actions including breakpoints (including `-k`/`-t` and persisted
ones), stepping, continue/pause, Evaluate-console entries, lines typed into
the Console view for the program's stdin (and Ctrl+D), stack-frame
navigation, variable expansion, restart, quit. The session is
written to `session.jsonl` as
JSON-RPC commands. Works with launch mode (any language) and `-r`
remote attach.

Replay it three ways:

- `tdb --replay-tui session.jsonl` opens the normal TUI on the recorded
  program and performs every recorded action in front of you, at the
  recorded pace: breakpoints appear in the Code View, steps and continues
  move the cursor, Evaluate entries echo into the console with their
  results, stack navigation and variable expansion play out in their
  panels. The title bar shows progress (`tdb ▶ session.jsonl 3/11`) and a
  toast announces each action (`--replay-quiet` turns those toasts off).
  A failed action raises an error toast and replay continues. When the recording does not end with `quit`, the
  debugger stays open at the final stop for you to carry on by hand.
  `--replay-interval S` waits a fixed S seconds before each action
  instead of reproducing the recorded gaps. `--replay-timeout S` bounds
  each stop-wait (default 30 s). Exit code 0
  iff every action succeeded. Don't type into the TUI while it is
  replaying: stray keystrokes interleave with the recorded actions.
- `tdb --replay session.jsonl` launches the recorded
  program headless, feeds every recorded command through the same RPC
  dispatch `tdb --server` uses, and prints a transcript (recorded time,
  command, verbatim result, interleaved program output). Exit code 0 iff
  every command succeeded. Add `--timing` to reproduce the original
  pacing or `--replay-interval S` for a fixed delay before each command,
  `--replay-timeout S` to bound each stop-wait (default 30 s).
- Against a live server: start `tdb --server prog.py`, then feed line 2
  onward of the file to `POST /rpc` . Each line is a valid request body:

      tail -n +2 session.jsonl | while read line; do
          curl -s -X POST -H 'Content-Type: application/json' \
               -d "$line" http://127.0.0.1:8150/rpc
      done

  (On Windows, an equivalent loop in Python: read the file, skip the
  first line, `requests.post` each remaining line.)

Not captured: pure viewing (scrolling, search, modals, thread/task
lists), breakpoint enable/disable toggles, variable expansions when the
adapter reports no `evaluateName` (currently the Perl adapter), and
File > Open program switches.
