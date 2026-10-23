# Perl

`tdb` bundles its own Perl adapter (`perl-tdb`) so only need
`perl` ≥ 5.18 on `PATH`. It drives stock `perl5db` under the
hood, so it works with any Perl already on the system.

The [PadWalker](https://metacpan.org/pod/PadWalker) module v2.5, by Robin Houston,
is bundled with `tdb`.  PadWalker provides a more complete view of lexical variables
in outer/caller frames and therefore provides a richer debugging experience.
PadWalker is an XS (compiled) module, so the bundled copy has to be built
against the exact perl that runs your program.  `tdb` does this for you on the
first Perl launch: if the perl in use cannot already load PadWalker (from CPAN
or a distro package), `tdb` compiles the bundled sources, caches the result
per interpreter under its config directory (`~/.config/tdb/padwalker/` on
Linux/macOS, `%APPDATA%\\tdb\\padwalker\\` on Windows), and prepends that
directory to the debuggee's `PERL5LIB`.  The build takes about a second and
needs perl's headers (`libperl-dev` on Debian/Ubuntu, `perl-devel` on
Fedora/RHEL), a C compiler, and the core `ExtUtils::MakeMaker` module (some
minimal distro perls omit it; `perl-ExtUtils-MakeMaker` on Fedora/RHEL);
Strawberry Perl on Windows ships all three.  When the build is not possible,
`tdb` prints a one-line notice on the console and falls back to a read-only
pad walk, so outer-frame lexicals degrade but debugging otherwise proceeds.
`tdb --info` reports the source directory, the cache directory, and the
`ExtUtils::MakeMaker` version of the `perl` it would use, with a warning when
that module is missing and PadWalker therefore cannot be built.
`TDB_PADWALKER_CACHE` overrides the cache location.
To build it ahead of time (for example when provisioning a machine, or to
see the compiler's error output without starting a debug session), run
`tdb --config install-padwalker`; it exits 0 when PadWalker loads
(natively, from the cache, or freshly built) and 1 with the reason when it
cannot be built.

**Launching a script:**

```bash
tdb script.pl
```

`--terminal` is supported (see
[External Terminal Support](../using/external-terminal.md)).

**Compile-time code (`BEGIN` blocks):** Perl runs `BEGIN` blocks and the
`use` statements that are themselves `BEGIN` blocks while it is still
*compiling* your program, before stock `perl5db` ever stops. `tdb` arms the
debugger ahead of compilation so that code is debuggable too, which means the
first stop is the first **compile-time** statement of your file (typically
`use strict;` near the top) rather than the first runtime statement. Step from
there and you land inside your `BEGIN` blocks, with the stack and evaluate
views working normally (local variable listing is limited at a compile-time
stop); the `Stack` view shows the frame as `main::BEGIN`. Stepping through a
`use` line takes a few steps (the pragma's
own compile-time work happens in between) but you are never dragged into
another module's internals.

Two consequences worth knowing:

- **Breakpoints are deferred while your program compiles.** During the compile
  phase Perl has only parsed part of your file, so its line table is
  incomplete and a breakpoint can't be verified yet. `tdb` holds such requests
  and, while it single-steps through the rest of compilation, checks each
  compile-time statement it lands on against them. A breakpoint placed
  *inside* a `BEGIN` block fires there directly, on the first run, without
  needing to be stepped into by hand. Conditional breakpoints work the same
  way at compile time; a condition that itself errors behaves exactly like a
  bad condition at runtime in that it does not fire. Two residual caveats: a
  breakpoint on a non-statement line (the `BEGIN {` line itself, or a blank
  line) never fires during the compile phase, since it's never actually
  trapped as a statement; and `hitCondition` (break on the Nth hit) isn't
  honored for a compile-time stop, only a plain `condition`.
- **Startup is slower for large dependency graphs**, because the debugger is
  active throughout compilation. A script with a big `use` tree takes
  noticeably longer to reach its first stop under `tdb`.

**`END` blocks** are entered with **step-in** (`s`). `next` and `continue` run
straight past them to program termination, which is standard `perl5db`
behavior, not a `tdb` limitation.

**When your program dies:** an uncaught Perl error (`die`, or a fatal runtime
error such as division by zero) opens the same error modal Python tracebacks
get: the message, the call stack parsed from Perl's `at FILE line N.` /
`... called at FILE line N` output, and Code View navigated to the failing
line. This works for compile-time failures too, including a `die` inside a
`BEGIN` block that aborts compilation. Press `e` in Code View to re-summon the
last error. `tdb` also reports the debuggee's real exit status rather than
assuming success.

**Remote attach:** useful when the Perl process is already running (a long-
lived service, a process started by something other than `tdb`) or lives on
another host/container. Add three lines to the target program, with the
`use` line first so the debugger is armed before any of your code compiles:

```perl
use Devel::TdbRemote;                 # first line of your program
...
Devel::TdbRemote::listen(5678);       # non-blocking
print "Waiting for tdb to attach on port 5678\n";
Devel::TdbRemote::wait_for_client();  # blocks until tdb connects
```

Then attach from `tdb`, forcing the language since there's no local `program`
argument for `tdb` to detect it from:

```bash
tdb --lang perl -r host:5678
```

**Arming caveat:** only code *compiled after* the debugger is armed can be
stepped into or breakpointed. That's why `use Devel::TdbRemote;` must be the
first line of the program. If you can't edit the first line (e.g. a wrapper
script controls startup), arm it before Perl even parses your file instead:
`perl -d:TdbRemote prog.pl`, or set `PERL5OPT=-d:TdbRemote` in the
environment that launches the debuggee.

**Copying the adapter to a remote host:** `Devel::TdbRemote` and its helper
script are plain files, not a CPAN install.  Copy both onto the remote
machine and point `PERL5LIB` at the directory that contains them, for example:

```bash
# From a checkout or an installed wheel's site-packages/tdb/adapters/perl:
scp -r Devel/TdbRemote.pm helpers.pl remote-host:/opt/tdb-perl/
# On the remote host:
export PERL5LIB=/opt/tdb-perl:$PERL5LIB
```

(`Devel/TdbRemote.pm` locates `helpers.pl` next to itself at runtime, so keep
the two files in the same relative layout shown above; `helpers.pl` is a
sibling of the `Devel/` directory, not inside it.)

**Pause in attach mode** uses a control channel: after the debug connection
is up, `tdb` opens a second connection to the same port and asks
`Devel::TdbRemote` to accept it. From then on `pause` (or the `p` key) writes a
byte on that connection, the kernel delivers `SIGIO` to the debuggee, and the
program stops at its next statement -- the same mechanism `perl5db` uses for
Ctrl-C, so a program blocked in `sleep` or `select` is interrupted too. The
second connection reuses the debug port, so an SSH tunnel that forwards that
one port carries both. Requirements: the `Devel/TdbRemote.pm` copied to the
remote host must be at least the version shipped with this feature (an older
copy still attaches fine, but `pause` returns a "not available" error and the
Console view says why), and the remote perl must support `O_ASYNC` (Linux,
macOS, BSD; not Windows).

**Live breakpoint hook:** the Perl counterpart of Python's
[`tdb.breakpoint()`](../python/breakpoint-hook.md). Run your program normally (not
under `tdb`) and have it open `tdb` on itself when it reaches a line of
interest:

```perl
use Devel::TdbRemote;                 # first line of your program
...
Devel::TdbRemote::breakpoint();       # tdb opens here, paused on the next line
```

On the first call the program listens on an ephemeral loopback port, spawns
`tdb --lang perl -r 127.0.0.1:PORT` on the same terminal, waits for it to
attach, and stops on the statement after the call, in your own frame. Later
calls reuse the running `tdb`, which simply receives another stop. Quitting
`tdb` (`Ctrl+q`) detaches and lets the program run on; a `breakpoint()` reached
after that spawns a fresh `tdb`. The call is a no-op when stdin/stdout are not
a tty, so it is safe in code that sometimes runs headless, and if `tdb` cannot
be started (not on `PATH`, or it exits before attaching) it warns and the
program continues. `tdb` is found on `PATH`, or via the `TDB` environment
variable. The same arming caveat as above applies: `use Devel::TdbRemote;`
must be the first line (or use `PERL5OPT=-d:TdbRemote`), and `Devel::TdbRemote`
has to be on `@INC`, e.g.

```bash
PERL5LIB=$(tdb --info | sed -n 's/.*Devel::TdbRemote dir *: *//p') perl prog.pl
```

See [`examples/Perl/breakpoint_hook_demo.pl`](https://github.com/AlDanial/tdb/blob/main/examples/Perl/breakpoint_hook_demo.pl).
