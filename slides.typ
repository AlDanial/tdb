// PyCon US lightning-talk slides for tdb.
//
// Build:    typst compile slides.typ
// Watch:    typst watch slides.typ
// Present:  any PDF reader in fullscreen (e.g. `mupdf -r 96 slides.pdf`)
//
// First compile auto-fetches the `touying` package from typst-universe.
// Requires Typst >= 0.11.

#import "@preview/touying:0.5.3": *
#import themes.metropolis: *

#show: metropolis-theme.with(
  aspect-ratio: "4-3",
  config-info(
    title: [`pip install textual-debugger`],
    subtitle: [`tdb`, a pure-Python TUI debugger],
    author: [Al Danial],
    date: [2026-05-14],
    institution: [PyCon US 2026 #sym.dot.c Lightning Talks],
  ),
)

#title-slide()

== `pip install textual-debugger`


- package `textual-debugger` provides module and command line tool  *`tdb`*
#v(1.0em)
- *Pure Python* text user interface (TUI) debugger
- Built on *#link("https://textual.textualize.io")[`textual`]* and *#link("https://github.com/microsoft/debugpy")[`debugpy`]*
  - has most of the capabilities of Microsoft VS Code Python Debugger extension
  - can do several things VS Code Python Debugger cannot
- Can be *100% keyboard driven* #sym.dash.em works over SSH, in tmux, anywhere a terminal does (mouse works too if available)

== What it looks like

#figure(
  image("gallery/toml_yaml.png", width: 90%),
)


== `tdb` provides the expected debugger experience

#v(0.6em)

- `tdb` provides the expected:
  - step through code
  - inspect (and modify) variables
  - evaluate expressions in scope
  - move up and down the call stack
  - set conditional breakpoints with optional hit counts
- Examine a *post-mortem traceback* after a crash
- Get a better `breakpoint()` #sym.dash.em drop in *`tdb.breakpoint()`* for the full TUI
- *Attach* to remotely running code

== `tdb` provides bonus features: inspect tasks, threads, and processes

#v(0.6em)

While the program is paused, peek into:

- *asyncio tasks* #sym.dash.em with a task dependency tree view (technically a DAG, not tree)
- *threads* #sym.dash.em every live `threading.Thread`
- *processes* #sym.dash.em children spawned by `concurrent.futures` or `multiprocessing`

#v(0.4em)

Switch focus to any of them, walk their frames, evaluate expressions in their scope.

== More bonus features:  server mode, headless mode, separate terminal for the debuggee

#v(0.6em)

- Debug via *JSON-RPC over HTTP* in server mode #sym.dash.em perfect for agentic debugging
- Run *headless* #sym.dash.em no TUI, just commands and responses (for CI / scripts)
- Run debuggee in a *separate terminal* #sym.dash.em avoids interference with `tdb` itself

== Try it

#v(1.8em)

#align(center)[
  #text(size: 30pt)[`pip install textual-debugger`]

  #v(1.2em)

  #text(size: 30pt)[`tdb my_program.py`]

  #v(1.2em)
  #text(size: 25pt)[- or without installing:]

  #text(size: 30pt)[`uvx --from textual-debugger tdb my_program.py`]
]

== Demos

- `tdb --doc`
- `tdb examples/least_squares.py`
  - basic stepping, variable inspection, expression evaluation
  - View focus, Alt v Ctrl key leaders
- `tdb examples/task_tree_v2.py`
  - c to run, immediately followeed by `p` to pause then inspect the task tree
- `tdb examples/asyncio_deadlock.py`
  - c to run, hangs, p to pause, inspect tasks and their dependencies, detect deadlock
- `tdb examples/threading_queue.py`
  - b lines 19 and 29, c to run, inspect threads and their frames
- `tdb examples/multiprocessing_pool.py`
  - b 19, c.  On breakpoint, inspect processes -- wait to load! and their frames
- `tdb --terminal xterm calculator.py`
