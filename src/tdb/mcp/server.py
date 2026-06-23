"""FastMCP server exposing tdb as a Model Context Protocol tool surface.

16 curated tools (not 26 auto-generated) so the agent's tool list stays
small and focused. Tool wrappers translate kwargs → JSON-RPC `params`
list and call through `McpSession._call`, which preserves the HTTP
dispatcher's lock policy — `pause` bypasses `session_lock` so an agent
can interrupt a runaway `control(action="continue")` mid-flight.

Stdio transport only. Single debug session per process. Events (DAP
SSE stream) are deferred — the `wait_for_stop` action and the
short-timeout `control` polling pattern make events unnecessary for v1.

SECURITY: the `inspect` tool calls debugpy's `evaluate`, which is
arbitrary Python execution in the debuggee process. MCP clients should
apply their permission models accordingly (Claude Desktop, IDE
extensions, etc. typically prompt before invoking tools).
"""

from __future__ import annotations

import logging
from typing import Literal

from mcp.server.fastmcp import FastMCP

from tdb.server.rpc_types import RpcResponse
from .session import McpSession

log = logging.getLogger(__name__)


# Single shared session for the entry-point's FastMCP instance. Tests
# construct their own session + FastMCP via `build_mcp(session=...)` so
# they don't collide with this module-level state.
_session = McpSession()


def _format(rsp: RpcResponse) -> str:
    """Render an RpcResponse as the string an MCP tool returns. Errors
    are prefixed `Error:` so agents distinguish failure from success
    payloads (e.g. the "still running" sentinel returned by `control`)."""
    if rsp.success:
        return rsp.value or "ok"
    return f"Error: {rsp.value}"


def _parse_breakpoints(specs: list[str] | None) -> list[tuple[str, int]] | None:
    """Convert ['file.py:42', ...] → [(path, line), ...]. Returns None
    when the input is None/empty so McpSession.launch can short-circuit."""
    if not specs:
        return None
    out: list[tuple[str, int]] = []
    for spec in specs:
        if ":" not in spec:
            raise ValueError(f"breakpoint spec must be 'file:line', got {spec!r}")
        file_part, line_part = spec.rsplit(":", 1)
        out.append((file_part, int(line_part)))
    return out


def _parse_path_mappings(
    mappings: list[list[str]] | list[tuple[str, str]] | None,
) -> list[tuple[str, str]] | None:
    """Accept [['local', 'remote'], ...] (JSON's array-of-arrays) and
    normalize to a list of tuples for the DAP client."""
    if not mappings:
        return None
    return [(str(pair[0]), str(pair[1])) for pair in mappings]


def build_mcp(session: McpSession | None = None) -> FastMCP:
    """Build a FastMCP instance wired to the given session (or the
    module-level shared one). Factored out so tests can construct an
    isolated server."""
    sess = session or _session
    mcp = FastMCP(
        "tdb",
        instructions=(
            "tdb is a Python debugger driven via DAP. Use `debug_launch` "
            "or `debug_attach` to start a session, `control` to step / "
            "continue / pause, and the `inspect` / `stack_trace` / `threads` / `tasks` / `processes` "
            "tools to examine state at a stopped frame. `control` and "
            "`wait_for_stop` return the sentinel 'still running — call "
            "pause or wait again' on timeout; respond with `control "
            "action=pause` to interrupt or `control action=wait_for_stop` "
            "to keep waiting. `inspect` executes arbitrary Python in the "
            "debuggee — surface that to the user before evaluating "
            "untrusted expressions."
        ),
    )

    # --- Lifecycle ----------------------------------------------------

    @mcp.tool(description="Start debugging a local Python program.")
    async def debug_launch(
        program: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        stop_on_entry: bool = False,
        just_my_code: bool = True,
        python: str | None = None,
        breakpoints: list[str] | None = None,
    ) -> str:
        """Launch `program` under the debugger. `breakpoints` is a list
        of 'file.py:42' specs set before the program starts running. If
        `stop_on_entry` is true the debuggee pauses at the first line;
        otherwise it runs until the first breakpoint or terminates.
        Returns a status line indicating where the debuggee landed."""
        return await sess.launch(
            program=program,
            args=args,
            cwd=cwd,
            stop_on_entry=stop_on_entry,
            just_my_code=just_my_code,
            python=python,
            breakpoints=_parse_breakpoints(breakpoints),
        )

    @mcp.tool(description="Attach to a remote debugpy server (host:port).")
    async def debug_attach(
        host: str,
        port: int,
        breakpoints: list[str] | None = None,
        path_mappings: list[list[str]] | None = None,
    ) -> str:
        """Attach to a debugpy server already listening on host:port.
        `path_mappings` is a list of [local_root, remote_root] pairs
        forwarded to debugpy for bidirectional path translation (use
        when tdb and the remote program have copies of the same code
        at different paths). The session pauses on attach so you can
        set breakpoints / inspect state immediately."""
        return await sess.attach(
            host=host,
            port=port,
            breakpoints=_parse_breakpoints(breakpoints),
            path_mappings=_parse_path_mappings(path_mappings),
        )

    @mcp.tool(description="Stop the current debug session.")
    async def quit() -> str:
        """Tear down the active session. Safe to call when no session
        is active. After `quit`, a fresh `debug_launch` / `debug_attach`
        can start a new session in the same MCP process."""
        return await sess.quit()

    # --- Control (single tool, consolidates 6 actions) ---------------

    @mcp.tool(description="Step, continue, pause, or wait for the next stop.")
    async def control(
        action: Literal[
            "continue",
            "next",
            "step_in",
            "step_out",
            "pause",
            "wait_for_stop",
        ],
        timeout_s: float = 30.0,
    ) -> str:
        """Drive the debuggee. `continue` / `next` / `step_in` /
        `step_out` issue a step and wait up to `timeout_s` for the
        next stop. On timeout, returns the sentinel 'still running —
        call pause or wait again'; respond by calling this tool again
        with `action="pause"` to interrupt, or `action="wait_for_stop"`
        to keep waiting without re-issuing the step. `pause` is special:
        it bypasses the session lock so it can interrupt a step / wait
        in flight from another tool call. Returns the new stop location
        (or program output if the debuggee terminated)."""
        params: list = []
        if action != "pause":
            params = [timeout_s]
        return _format(await sess._call(action, params))

    # --- Inspection ---------------------------------------------------

    @mcp.tool(description="Evaluate one or more expressions in the stopped frame.")
    async def inspect(expressions: list[str]) -> str:
        """Evaluate each expression against the current stopped frame
        and return the results formatted as 'expr = value' lines. THIS
        EXECUTES ARBITRARY PYTHON in the debuggee process — use only
        when the user has authorized inspection of the running program."""
        return _format(await sess._call("inspect", list(expressions)))

    @mcp.tool(description="Read the contents of a source file.")
    async def read_source(file_path: str) -> str:
        """Return the contents of `file_path` on the local filesystem
        (resolved to an absolute path). Use to see source around a
        stop location reported by `control` or `stack_trace`."""
        return _format(await sess._call("get_source", [file_path]))

    @mcp.tool(description="Show the current stack trace.")
    async def stack_trace() -> str:
        """Return the call stack at the current stop, with the active
        frame marked with `*`. Use `inspect` to read locals from the
        active frame; switch frames with stack navigation if the tdb
        server exposes it (not currently part of this MCP surface)."""
        return _format(await sess._call("get_stack_trace", []))

    @mcp.tool(
        description="Report whether the debuggee is stopped, running, or terminated."
    )
    async def status() -> str:
        """Return one of: 'running', 'terminated', or 'stopped (<reason>)
        at <file>:<line>'. Cheap call — use to confirm session state
        before issuing other tools."""
        return _format(await sess._call("status", []))

    @mcp.tool(description="Drain captured stdout/stderr from the debuggee.")
    async def get_output() -> str:
        """Return and clear all stdout/stderr captured since the last
        drain. Output is also folded into the return value of `control`
        when the program terminates or stops; this tool is for peeking
        without stepping."""
        return _format(await sess._call("get_output", []))

    # --- Breakpoints --------------------------------------------------

    @mcp.tool(description="Set a breakpoint at 'file:line', with optional condition.")
    async def set_breakpoint(
        spec: str,
        condition: str | None = None,
        hit_condition: str | None = None,
    ) -> str:
        """`spec` is 'file.py:42'. `condition` is a Python expression
        that must evaluate truthy for the breakpoint to fire.
        `hit_condition` is an integer (or expression) gating on hit
        count. Breakpoints snap to the nearest logical-statement start;
        the response reports the move if any."""
        params: list = [spec]
        if condition is not None:
            params.append(condition)
            if hit_condition is not None:
                params.append(hit_condition)
        elif hit_condition is not None:
            params.extend(["", hit_condition])
        return _format(await sess._call("set_breakpoint", params))

    @mcp.tool(description="Remove the breakpoint at 'file:line'.")
    async def remove_breakpoint(spec: str) -> str:
        return _format(await sess._call("remove_breakpoint", [spec]))

    @mcp.tool(description="List all active breakpoints.")
    async def list_breakpoints() -> str:
        return _format(await sess._call("list_breakpoints", []))

    # --- Differentiators: threads / tasks / processes / wait_graph ---

    @mcp.tool(description="List threads or inspect one (stack + locals).")
    async def threads(thread_id: int | None = None) -> str:
        """No `thread_id` → list every thread with `id: name` and `*`
        marking the current one. With `thread_id` → return that
        thread's stack frames and locals."""
        if thread_id is None:
            return _format(await sess._call("list_threads", []))
        return _format(await sess._call("inspect_thread", [thread_id]))

    @mcp.tool(description="List asyncio tasks or inspect one.")
    async def tasks(task_name: str | None = None) -> str:
        """No `task_name` → list every task with state and what it's
        awaiting. With `task_name` → return that task's stack and
        locals. Pair with `wait_graph` to diagnose hangs."""
        if task_name is None:
            return _format(await sess._call("list_tasks", []))
        return _format(await sess._call("inspect_task", [task_name]))

    @mcp.tool(description="List child processes (multiprocessing) or inspect one.")
    async def processes(name_or_pid: str | None = None) -> str:
        """No `name_or_pid` → list every tracked child process. With
        `name_or_pid` → return that process's metadata. tdb attaches
        to children via debugpy's subProcess mechanism."""
        if name_or_pid is None:
            return _format(await sess._call("list_processes", []))
        return _format(await sess._call("inspect_process", [name_or_pid]))

    @mcp.tool(
        description=(
            "Show asyncio task wait-graph and any deadlock cycles "
            "— the 'why is my program hung?' tool."
        )
    )
    async def wait_graph() -> str:
        """Returns blocked tasks with their awaiting target and
        identified holders, plus any deadlock cycles. The single best
        tool for diagnosing async hangs from an agent."""
        return _format(await sess._call("wait_graph", []))

    return mcp


def main() -> None:
    """Entry point for `tdb-mcp` / `python -m tdb.mcp`. Starts FastMCP
    on stdio — control returns when the client disconnects."""
    logging.basicConfig(level=logging.WARNING)
    mcp = build_mcp()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
