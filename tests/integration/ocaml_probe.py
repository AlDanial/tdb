"""Manual probe: answers the 5 open questions in the OCaml design spec.

Run:  uv run python tests/integration/ocaml_probe.py
Not a pytest test. Prints findings as labeled sections; copy them into
docs/superpowers/specs/2026-08-22-ocaml-support-design.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


# ---------- native / lldb-dap side (reuses tdb's cpp profile) ----------


async def probe_lldb() -> None:
    from tdb.dap.types import SourceBreakpoint
    from tdb.languages.cpp import build_cpp_profile
    from tdb.server.event_handler import ServerEventHandler
    from tdb.session.controller import DebugController

    exe = FIXTURES / "ocaml_domains.exe"
    subprocess.run(
        ["ocamlopt", "-g", "-o", str(exe), "ocaml_domains.ml"],
        cwd=FIXTURES,
        check=True,
    )
    # Same harness idiom as tests/integration/test_cpp_session.py's _launch:
    # breakpoints are seeded into controller.state BEFORE ctrl.start() is
    # called (start() reads state.breakpoints to build the initial
    # setBreakpoints requests during configuration).
    handler = ServerEventHandler()
    controller = DebugController(handler, profile=build_cpp_profile(adapter="lldb-dap"))
    src = str(FIXTURES / "ocaml_domains.ml")
    # Breakpoint on the Atomic.incr line (line 5) -- hit from a spawned domain.
    controller.state.breakpoints.setdefault(src, []).append(SourceBreakpoint(line=5))
    await controller.start(program=str(exe), stop_on_entry=False)
    await asyncio.wait_for(handler.initialized_event.wait(), 30)
    await controller.do_configure()
    assert await handler.wait_for_stop(30), "breakpoint never hit"
    print(
        f"  [diag] last_stop_reason={handler.last_stop_reason!r} "
        f"thread_id={handler.last_stop_thread_id!r} "
        f"description={handler.last_stop_description!r} "
        f"terminated={handler.terminated_event.is_set()}"
    )
    print(f"  [diag] captured stdout so far: {handler.drain_output()!r}")

    # lldb-dap's thread list is NOT immediately complete when the stopped
    # event fires -- observed: 1 thread at t+0s, growing to the full set
    # ~1-2s later as lldb finishes enumerating OS threads. Poll briefly
    # for it to stabilize before treating the list as authoritative.
    threads = await controller.client.threads()
    for _ in range(10):
        await asyncio.sleep(0.5)
        threads2 = await controller.client.threads()
        if len(threads2) == len(threads):
            break
        threads = threads2
    print("== Q3: thread naming (raw DAP threads while stopped) ==")
    print(f"  total thread count: {len(threads)}")
    for t in threads:
        frames = await controller.client.stack_trace(t.id, levels=30)
        top = [f.name for f in frames[:6]]
        print(f"  id={t.id} name={t.name!r} top_frames={top}")
        print(
            f"    [diag] full stack ({len(frames)} frames): {[f.name for f in frames]}"
        )

    print("== Q2: DWARF locals in an OCaml frame ==")
    for t in threads:
        frames = await controller.client.stack_trace(t.id)
        print(f"  -- thread id={t.id} name={t.name!r} --")
        for f in frames[:6]:
            try:
                scopes = await controller.client.scopes(f.id)
                for s in scopes:
                    var_list = await controller.client.variables(s.variables_reference)
                    print(
                        f"  frame={f.name!r} scope={s.name}: "
                        f"{[(v.name, v.value, v.type) for v in var_list]}"
                    )
            except Exception as e:
                print(f"  frame={f.name!r}: scopes failed: {e}")

    print("== Q5: caml_fatal_uncaught_exception symbol ==")
    nm = subprocess.run(["nm", str(exe)], capture_output=True, text=True)
    hits = [l for l in nm.stdout.splitlines() if "fatal_uncaught" in l]
    print(f"  nm hits: {hits or 'NONE — record fallback'}")
    lldb_batch = subprocess.run(
        ["lldb", "-b", "-o", "b caml_fatal_uncaught_exception", str(exe)],
        capture_output=True,
        text=True,
    )
    print(f"  lldb batch breakpoint output:\n{lldb_batch.stdout}")

    await controller.stop()


# ---------- bytecode / earlybird side (raw stdio DAP) ----------


class RawDap:
    """Minimal Content-Length-framed DAP over a subprocess's stdio."""

    def __init__(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.seq = 0

    def send(self, command: str, arguments: dict | None = None) -> int:
        self.seq += 1
        msg = {"seq": self.seq, "type": "request", "command": command}
        if arguments is not None:
            msg["arguments"] = arguments
        # Compact JSON (no space after ":"/",") — earlybird's DAP framing
        # parser (the opam `dap` library) misparses a body containing the
        # `": "` separator json.dumps emits by default, consuming it as
        # more header lines and blocking forever (see the spec's Critical
        # caveat / follow-up root-cause paragraphs). Compact framing avoids
        # that byte sequence entirely and round-trips instantly.
        raw = json.dumps(msg, separators=(",", ":")).encode()
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n%s" % (len(raw), raw))
        self.proc.stdin.flush()
        return self.seq

    def recv(self, timeout: float = 15.0) -> dict:
        # select()-bounded framed read. Kept even though the root cause is
        # now known and fixed (see `send()`'s compact-JSON comment): a
        # malformed/non-compact framing from some other code path would
        # otherwise hang this call forever, so the timeout stays as a
        # belt-and-suspenders guard rather than something to rely on.
        deadline = time.monotonic() + timeout
        header = b""
        while b"\r\n\r\n" not in header:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "no response within "
                    f"{timeout}s (see Q1 finding: earlybird appears to "
                    "deadlock when driven via Python subprocess pipes)"
                )
            r, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not r:
                continue
            # Read the raw fd (not self.proc.stdout.read()): a
            # BufferedReader drains the whole pending pipe buffer into
            # its own userspace buffer on the first read, returning only
            # the 1 byte asked for -- so a later select() on the fd sees
            # "not ready" (already drained at the OS level) even though
            # more bytes are sitting unread in the Python buffer, and
            # this loop would spin on `continue` until timeout. Reading
            # the fd directly keeps select() and read() at the same
            # layer.
            b1 = os.read(self.proc.stdout.fileno(), 1)
            if not b1:
                raise EOFError(self.proc.stderr.read().decode())
            header += b1
        length = int(header.split(b"Content-Length:")[1].split(b"\r\n")[0])
        body = b""
        while len(body) < length:
            body += os.read(self.proc.stdout.fileno(), length - len(body))
        return json.loads(body)

    def recv_until(self, pred) -> dict:
        while True:
            msg = self.recv()
            print(
                f"    <- {msg.get('type')} "
                f"{msg.get('command') or msg.get('event')}: "
                f"{json.dumps(msg)[:300]}"
            )
            if pred(msg):
                return msg


def probe_earlybird() -> None:
    byte = FIXTURES / "ocaml_fatal.byte"
    subprocess.run(
        ["ocamlc", "-g", "-o", str(byte), "ocaml_fatal.ml"], cwd=FIXTURES, check=True
    )
    print("== Q1: earlybird invocation + launch fields ==")
    help_out = subprocess.run(
        ["ocamlearlybird", "--help"], capture_output=True, text=True
    )
    print(help_out.stdout or help_out.stderr)

    dap = RawDap(["ocamlearlybird", "debug"])
    try:
        dap.send("initialize", {"adapterID": "probe", "clientID": "tdb"})
        init = dap.recv_until(lambda m: m.get("command") == "initialize")
        print(f"  capabilities: {json.dumps(init.get('body', {}), indent=2)}")
        # Try the field names the plan assumes; if the response is an error,
        # its message names the expected schema — record it.
        dap.send(
            "launch",
            {
                "program": str(byte),
                "arguments": [],
                "cwd": str(FIXTURES),
                "stopOnEntry": True,
                "console": "internalConsole",
            },
        )
        dap.recv_until(
            lambda m: m.get("event") == "initialized" or m.get("command") == "launch"
        )
        dap.send("configurationDone")
        stopped = dap.recv_until(
            lambda m: m.get("event") == "stopped" or m.get("event") == "terminated"
        )
        if stopped.get("event") == "stopped":
            print("== Q4: pause support ==")
            dap.send("threads")
            tmsg = dap.recv_until(lambda m: m.get("command") == "threads")
            tid = tmsg["body"]["threads"][0]["id"]
            dap.send("continue", {"threadId": tid})
            dap.recv_until(lambda m: m.get("command") == "continue")
            dap.send("pause", {"threadId": tid})
            print("  (watch whether a 'stopped' event or an error follows)")
            dap.recv_until(
                lambda m: (
                    m.get("event") in ("stopped", "terminated")
                    or m.get("command") == "pause"
                )
            )
    except TimeoutError as e:
        # This used to be the expected, documented outcome: earlybird's
        # DAP framing parser (the opam `dap` library) misparses a body
        # containing json.dumps's default `": "` separator, so it never
        # answered `initialize` when driven from Python. `send()` now
        # emits compact JSON (no `": "` byte sequence), which fixes that
        # -- so reaching this branch again means something NEW is wrong,
        # not the historical framing bug. See the spec's Critical caveat
        # / follow-up root-cause paragraphs for the full story.
        print(f"  Q1/Q4 BLOCKED (unexpected -- framing fix did not help): {e}")
    finally:
        dap.proc.kill()


if __name__ == "__main__":
    if shutil.which("lldb-dap") and shutil.which("ocamlopt"):
        asyncio.run(probe_lldb())
    else:
        print("SKIP lldb side: need lldb-dap + ocamlopt")
    if shutil.which("ocamlearlybird") and shutil.which("ocamlc"):
        probe_earlybird()
    else:
        print("SKIP earlybird side: need ocamlearlybird + ocamlc")
