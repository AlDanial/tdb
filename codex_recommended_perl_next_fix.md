
- The cause is now isolated. With the compile-phase shim enabled, the first 40 stops in `cloc` include repeated sequences
such as 36,36,36, 37,37,37, and 39,39,39; all are in Perl’s `START` phase. Running the same adapter with only that shim
disabled yields 32,33,34,53,56,… in `RUN`, with one stop per displayed line. This also dates the regression to the
compile-time/`BEGIN`-block stepping feature, not the Code View keybinding.


Root cause confirmed: the repeated `next` presses come from Perl’s compile-phase stepping shim, not Code View or DAP
command handling.

`n` follows this path:

- Code View sends one `step_over`.
- The controller sends one DAP `next`.
- The Perl adapter sends one literal `n` to `perl5db`.

The problem begins in `src/tdb/adapters/perl/session.py:136`, which preloads `src/tdb/adapters/perl/Devel/
TdbCompile.pm:19`. That shim arms `$DB::single` while Perl is compiling the target so `BEGIN` blocks are debuggable.

During compilation, one source statement can generate several internal compile-time operations. A `use` statement, for
example, expands conceptually into loading the module and invoking its import logic. `perl5db` stops for multiple such
operations even though they all carry the same source-line number. The adapter currently reports every one as a
separate DAP stop in `src/tdb/adapters/perl/server.py:534`.

Observed against `./tdb cloc`:

START: 36, 36, 36   # use warnings;
START: 37, 37, 37   # use strict;
START: 39, 39, 39   # use Getopt::Long;
START: 40, 40, 40   # use File::Basename;
...

With only `TdbCompile.pm` disabled, stock runtime stepping produced:

RUN: 32, 33, 34, 53, 56, 57, 58, 59, 65, ...

Thus the shim is necessary for `BEGIN` debugging, but its internal stops are leaking through as user-visible steps. The
existing tests only check that stepping eventually reaches a desired line; they do not assert that every next changes
the displayed line, so this escaped coverage.

Fix plan:

1. Add a small Perl integration fixture containing several `use` statements and a `BEGIN` block.
2. Add a regression test proving one DAP `next` advances to a different displayed source line during `START`, while
   preserving the ability to enter and inspect a `BEGIN` block.

3. In the Perl adapter, remember the originating file and line for a user-issued `next`.
4. When the resulting stop is classified, if it is still in phase `START` at the same file and line—and it is not a
   breakpoint or pause—issue another internal `n` and suppress the intermediate DAP `stopped` event.

5. Bound that internal loop to prevent a malformed/debugger-specific stop sequence from running indefinitely.
6. Restrict the coalescing to compile-phase `next`. Applying it indiscriminately during `RUN` could incorrectly skip
   repeated executions of a one-line loop.

7. Run the Perl adapter/session suites and manually verify `./tdb cloc`, especially the `use` block and transition from
   `START` to `RUN`.

