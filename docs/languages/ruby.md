# Ruby

tdb debugs Ruby via the [debug gem](https://github.com/ruby/debug)'s
`rdbg` (Ruby >= 3.1 ships it; otherwise `gem install debug`; tdb needs
debug >= 1.9). `rdbg` must be on PATH, or point tdb at it with
`{"adapters": {"rdbg": "/path/to/rdbg"}}` in config.json.

```bash
tdb script.rb                # launch, stop at first line
tdb --run script.rb          # run immediately, debug on demand
tdb --terminal script.rb     # program I/O in its own terminal
```

Remote attach: start the program with
`rdbg --open --port 5678 --host 0.0.0.0 script.rb` (add `--nonstop` to
let it run before you attach), then `tdb -r HOST:5678 --lang ruby`.
Note: rdbg's `--cookie` authentication is not part of DAP and is not
supported; bind to localhost and tunnel over SSH instead.
`--local-root`/`--remote-root` path mappings are not supported for Ruby
yet. Bundler projects work when your environment resolves `rdbg`
(`gem install debug` into the project's Ruby); there is no `bundle
exec` integration yet.

> **Startup note:** the debug gem's `rdbg` has an occasional handshake
> race on launch; tdb detects a stalled/corrupted handshake and retries
> once automatically with a fresh `rdbg`, so this is usually invisible.

**Live breakpoint hook:** the Ruby counterpart of Python's
[`tdb.breakpoint()`](../python/breakpoint-hook.md). Run your program normally (not
under `tdb`) and have it open `tdb` on itself when it reaches a line of
interest:

```ruby
require 'tdb'
...
Tdb.breakpoint          # tdb opens here, paused on the next line
```

The first call starts the debug gem's own debug server on an ephemeral
loopback port and plants the gem's usual one-shot breakpoint on the next
line. When that fires, the program parks and `tdb --lang ruby -r 127.0.0.1:PORT`
is spawned on the same terminal, attaches, and shows the stop in your own
frame. Later calls reuse the running `tdb`. Quitting `tdb` (`Ctrl+q`) detaches
and lets the program run on; a `Tdb.breakpoint` reached after that spawns a
fresh `tdb`. The call is a no-op when stdin/stdout are not a tty, and if `tdb`
is not on `PATH` (or in the `TDB` environment variable) it warns and the
program continues. Put `tdb.rb` on the load path, e.g.

```bash
RUBYLIB=$(tdb --info | sed -n 's/.*tdb.rb dir *: *//p') ruby prog.rb
```

See [`examples/Ruby/breakpoint_hook_demo.rb`](https://github.com/AlDanial/tdb/blob/main/examples/Ruby/breakpoint_hook_demo.rb).
