# About

## Tech Stack

- [textual](https://github.com/Textualize/textual) : TUI framework
- [debugpy](https://github.com/microsoft/debugpy) : Debug Adapter Protocol implementation for Python
- [gdb](https://sourceware.org/gdb/) / [lldb-dap](https://lldb.llvm.org/resources/lldbdap.html) : optional, user-installed DAP adapters for C/C++/Fortran
- [debug gem](https://github.com/ruby/debug) (`rdbg`) : optional, user-installed DAP adapter for Ruby
- [ocamlearlybird](https://github.com/hackwaly/ocamlearlybird) : optional, user-installed DAP adapter for OCaml bytecode (native OCaml reuses `gdb`/`lldb-dap`)
- [pygments](https://pygments.org/) : Syntax highlighting
- [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) : JSON-RPC server

## License

MIT

## Acknowledgments

Thank you:

- Will McGugan for the amazing `textual` module.
`tdb` would be a pale shadow of itself had I used any other TUI framework.
Fantastic work, Will.

- Microsoft for the Debug Adapter Protocol (DAP) and releasing
its implementation in `debugpy` and the Python Debugger extension for Visual Studio Code
as open source.

- Anthropic, for providing access to Claude Code through the
[Claude for Open Source](https://claude.com/contact-sales/claude-for-oss) program.
`tdb` was made almost entirely with Claude Code.

- OpenAI, for providing access to Codex through the
[Codex for Open Source](https://developers.openai.com/community/codex-for-oss) program.
Codex was used to implement tcsh and Rust support.

This project was inspired by Andreas Klöckner's excellent [pudb](https://pypi.org/project/pudb/)
Python debugger.


## Known Problems

This command
```
tdb --terminal gnome-terminal --python /path/to/venv/matplotlib/bin/python3 examples/double_pendulum.py
```
either ignores breakpoints or crashes after showing the first frame.
The `--python` argument must point to an installation with `matplotlib`.
