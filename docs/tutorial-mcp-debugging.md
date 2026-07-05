# Debugging a Python Program with an AI Agent and tdb (MCP Mode)

This tutorial shows how to prompt an agentic AI system
to find runtime bugs in a
Python program using tdb's Model Context Protocol server. Instead of the
agent guessing from source code or inserting `print()` statements and/or
writing to log files, it
sets breakpoints, runs the program, and inspects live variables until
the evidence pins down the bug.

The flawed program for this exercise is
a sales-report script that produces a grand total three times too large.
The program contains two
separate bugs, neither of which raises an exception.
The output is incorrect though.

## 1. Setup

Install tdb and register the MCP server with your agent. From a
checkout:

```bash
cd tdb
uv pip install -e .
```

**Claude Code** (one-time registration):

```bash
claude mcp add tdb -- tdb-mcp
```

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tdb": {
      "command": "tdb-mcp"
    }
  }
}
```

`tdb-mcp`, `tdb --mcp`, and `python -m tdb.mcp` are equivalent — use
whichever your MCP client launches most easily. The agent now has 16
debugging tools: `debug_launch`, `debug_attach`, `quit`, `control`,
`inspect`, `read_source`, `stack_trace`, `status`, `get_output`,
`set_breakpoint`, `remove_breakpoint`, `list_breakpoints`, `threads`,
`tasks`, `processes`, and `wait_graph`.

## 2. The buggy program

The tutorial program lives at `examples/sales_report_buggy.py`. It
groups sales line items by category, applies a 10%-off bulk discount to
orders of 10 or more units, and prints per-category subtotals.

Run it:

```
$ python examples/sales_report_buggy.py
bakery   7 items   subtotal $  44.90
dairy    7 items   subtotal $  44.90
fruit    7 items   subtotal $  44.90
grand total: $134.70
```

Every category claims to contain all seven line items, every subtotal
equals the whole order, and the grand total is triple what it should
be. Adding the seven line items by hand gives $44.40. Nothing
crashes; the code reads plausibly. Time to hand it to the agent.

> The transcripts below abbreviate the checkout path as
> `/home/user/tdb`. Substitute your own absolute path — tdb breakpoints
> work best with absolute file paths.

## 3. Writing the prompt

How you phrase the request matters. Compare:

**Weak prompt:**

> fix examples/sales_report_buggy.py

The agent will probably just read the source and pattern-match. It may
find one bug, miss the other, and you get no runtime evidence that the
diagnosis is correct.

**Good prompt:**

> `examples/sales_report_buggy.py` prints a grand total of $134.70, but
> summing the line items by hand gives $44.40 — and every category
> claims to have all 7 items. Use the tdb debugger tools to diagnose
> this at runtime: set breakpoints, run it, and show me the variable
> values that prove the root cause before you change any code. After
> fixing, re-run it under the debugger and confirm every subtotal and
> the grand total are right.

The good prompt has four important parts:

1. **The symptom is quantified.** "$134.70 instead of $44.40" and "7
   items in every category" gives the agent two precise anomalies to
   resolve, not just "it's wrong."
2. **The expected behavior.** Without the hand-checked $44.40 the agent
   has no reference value to verify against.
3. **A demand for runtime evidence.** "Show me the variable values that
   prove the root cause" pushes the agent to use `inspect` at a
   breakpoint rather than eyeball the source. This is what catches the
   *second* bug — which code-reading routinely misses.
4. **A verification requirement.** "Re-run it under the debugger and
   confirm" means the fix isn't declared done on vibes.

## 4. What the agent does: finding bug #1

Below is a representative tool-call transcript. Your agent's exact
actions will vary, but the pattern, *breakpoint where the numbers are
born, inspect, chase the anomaly upstream*, should be similar.

The subtotals are computed at line 42 of `build_report()`, so the agent
launches with a breakpoint there. `debug_launch` returns as soon as the
program is running; `wait_for_stop` follows it to the breakpoint:

```
debug_launch(
    program="/home/user/tdb/examples/sales_report_buggy.py",
    breakpoints=["/home/user/tdb/examples/sales_report_buggy.py:42"])
→ Session launched; debuggee is running.

control(action="wait_for_stop", timeout_s=30)
→ /home/user/tdb/examples/sales_report_buggy.py:42
```

Stopped at the first subtotal. What's in this category's group?

```
inspect(expressions=["category", "len(group)", "[i['name'] for i in group]"])
→ category = 'bakery'
  len(group) = 7
  [i['name'] for i in group] = ['apples', 'bananas', 'cherries', 'milk',
                                'yogurt', 'bread', 'bagels']
```

The bakery group contains apples and milk so grouping is broken. The
`groups` dict is in scope, so the agent tests the a hypothesis:
are the buckets actually distinct lists?

```
inspect(expressions=["groups['fruit'] is groups['bakery']",
                     "id(groups['fruit'])", "id(groups['dairy'])"])
→ groups['fruit'] is groups['bakery'] = True
  id(groups['fruit']) = 140699003561664
  id(groups['dairy']) = 140699003561664
```

**Smoking gun.** All three categories share one list object. Every
`append` lands in every bucket. The agent reads the grouping code to
find where the aliasing is created:

```
read_source(file_path="/home/user/tdb/examples/sales_report_buggy.py")
→ ...
  31    groups = dict.fromkeys(categories, [])
  ...
```

`dict.fromkeys(keys, [])` evaluates the `[]` **once** and binds every
key to that single list — a classic Python trap. The fix is a dict
comprehension that builds a fresh list per key:

```python
# line 31, before:
groups = dict.fromkeys(categories, [])
# after:
groups = {category: [] for category in categories}
```

The agent ends the session, applies the edit, and re-verifies:

```
quit()
→ Session stopped.
```

## 5. The second bug: why "verify under the debugger" matters

After fixing line 31, the agent re-runs the program:

```
debug_launch(program="/home/user/tdb/examples/sales_report_buggy.py")
→ Session launched; debuggee is running.

control(action="wait_for_stop", timeout_s=30)
→ bakery   2 items   subtotal $  15.60
  dairy    2 items   subtotal $  13.60
  fruit    3 items   subtotal $  15.70
  grand total: $44.90
```

(The program ran to completion, so the stop report carries its output.)
Grouping is fixed — but the prompt said the expected total is **$44.40**
and this says **$44.90**. An agent that skipped verification would have
shipped a half-fix here. Checking category math against the data: fruit
should be `apples 10 × $0.50 with 10% bulk discount = $4.50`, plus
bananas $2.70, plus cherries $8.00 = **$15.20**, not $15.70. Apples —
an order of *exactly* 10 units — didn't get its discount. That looks
like a boundary condition in `line_total()`.

This is the moment for a **conditional breakpoint**: break in
`line_total` only for the suspicious quantity, skipping the six
irrelevant calls. The agent launches with `stop_on_entry` so the
program is paused while it arms the breakpoint (launching free-running
and *then* setting a breakpoint is a race — a short script can finish
first):

```
debug_launch(
    program="/home/user/tdb/examples/sales_report_buggy.py",
    stop_on_entry=True)
→ Session launched; stopped at /home/user/tdb/examples/sales_report_buggy.py:1

set_breakpoint(spec="/home/user/tdb/examples/sales_report_buggy.py:23",
               condition="qty == 10")
→ ok

control(action="continue", timeout_s=30)
→ /home/user/tdb/examples/sales_report_buggy.py:23

inspect(expressions=["qty", "BULK_QTY", "qty > BULK_QTY", "qty >= BULK_QTY"])
→ qty = 10
  BULK_QTY = 10
  qty > BULK_QTY = False
  qty >= BULK_QTY = True
```

Line 23 reads `if qty > BULK_QTY:` but the spec (and the comment on
`BULK_DISCOUNT`) says orders of `BULK_QTY` **or more** get the
discount. Stepping confirms the branch is skipped:

```
control(action="next")
→ /home/user/tdb/examples/sales_report_buggy.py:25
```

Execution jumped straight from the `if` on line 23 to the `return` on
line 25 — the discount on line 24 never ran. Fix: `>` → `>=`.

## 6. Final verification

```
quit()
→ Session stopped.

debug_launch(program="/home/user/tdb/examples/sales_report_buggy.py")
→ Session launched; debuggee is running.

control(action="wait_for_stop", timeout_s=30)
→ bakery   2 items   subtotal $  15.60
  dairy    2 items   subtotal $  13.60
  fruit    3 items   subtotal $  15.20
  grand total: $44.40

quit()
→ Session stopped.
```

$44.40 — matches the hand check. Both bugs found with runtime
evidence, both fixes verified by execution, not inspection.

## 7. Prompting patterns that generalize

- **Quantify the symptom and state the expected value.** "Returns 134.70,
  should return 44.40" gives the agent an oracle. "It's broken" gives it
  a guessing game.
- **Ask for evidence before edits.** "Show me the variable values that
  prove the root cause" forces `inspect` at a breakpoint. Agents that
  diagnose by reading source alone routinely fix one bug and miss the
  boundary condition next to it.
- **Require re-verification under the debugger.** The second bug in this
  tutorial is only caught because the first fix was re-run and compared
  against the expected total.
- **Point at the anomaly's birthplace if you know it.** "The subtotals
  are computed in `build_report` — start there" saves the agent a lap.
  If you don't know, `stack_trace()` from any breakpoint orients it.
- **For hangs, name the tools.** "Use `wait_graph` to see what the tasks
  are waiting on" or "if `continue` reports *still running*, use
  `control(action='pause')` to interrupt it and see where it's stuck."
  The pause tool works even while another `control` call is blocked —
  a runaway loop can always be interrupted.

## 8. Notes and caveats

- **Absolute paths.** Breakpoint specs and `program` should be absolute
  paths; agents usually handle this, but relative paths in your prompt
  can propagate into failed breakpoints.
- **`inspect` executes arbitrary Python in the debuggee.** That is what
  a debugger's evaluate does--but it means approving an `inspect` call
  is approving code execution. Review the expressions your agent
  proposes, especially on programs that touch credentials or production
  state.
- **Breakpoints snap to statement starts.** If a breakpoint lands
  mid-statement, tdb moves it to the nearest logical statement start and
  says so in the response.
- **One session per server.** `debug_launch` while a session is active
  returns an error; the agent must `quit()` first. After editing the
  program on disk, a relaunch picks up the new code.
- **Timeouts are normal.** `control` returning
  `still running — call pause or wait again` is not an error; the
  program just didn't stop within `timeout_s`. Waiting again
  (`wait_for_stop`) or interrupting (`pause`) are both legitimate next
  moves, and the agent should choose based on whether the program is
  expected to still be working.

