"""Runs debugpy's adapter, accepting only a debuggee that has tdb's token.

For `tdb -a` the adapter waits on a local port for the injected code to dial
back, and anything on the machine can connect first and pose as the program.
debugpy can check a token, but its "listen" attach clears it just before
serving, so this puts the token back at that moment. The test in
tests/integration/test_adapter_token.py fails if debugpy moves that.

Run as a script under `-P`, with the module to run as the argument, so that
it needs nothing from tdb. The token comes in the environment, as any local
user can read a command line.
"""

import os
import runpy
import sys

TOKEN_VARIABLE = "TDB_ADAPTER_TOKEN"


def require_token(token: str) -> None:
    # The adapter closes stderr off before it first imports its logging, which
    # keeps hold of the stream. Here the same imports come first.
    stderr = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        from debugpy import adapter
        # In the adapter's own order: servers first is a circular import.
        from debugpy.adapter import clients, servers  # noqa: F401
    finally:
        sys.stderr = stderr

    serve = servers.serve

    def serve_with_token(*args, **kwargs):
        adapter.access_token = token
        return serve(*args, **kwargs)

    servers.serve = serve_with_token


def main() -> None:
    module = sys.argv.pop(1)
    token = os.environ.pop(TOKEN_VARIABLE, None)
    if not token:
        sys.exit(f"tdb: {TOKEN_VARIABLE} is not set; refusing to start the adapter")
    require_token(token)
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
