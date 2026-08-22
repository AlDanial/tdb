"""Manual probe: answers the 5 open questions in the OCaml design spec.

Run:  uv run python tests/integration/ocaml_probe.py
Not a pytest test. Prints findings as labeled sections; copy them into
docs/superpowers/specs/2026-08-22-ocaml-support-design.md.
"""

from __future__ import annotations

import asyncio
import json
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
        raw = json.dumps(msg).encode()
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n%s" % (len(raw), raw))
        self.proc.stdin.flush()
        return self.seq

    def recv(self, timeout: float = 15.0) -> dict:
        # select()-bounded framed read. NOT purely cosmetic: probing found
        # that ocamlearlybird 1.3.6, when spawned via Python's subprocess
        # module (sync Popen *and* asyncio.create_subprocess_exec, plain
        # pipes and a pty both tried), never produces a response to
        # `initialize` even though `strace` shows its internal reader
        # thread genuinely read() the request bytes off fd 0. The same
        # exchange succeeds instantly via a shell redirect/FIFO or via
        # Node's child_process.spawn. Root cause not identified (ruled
        # out: EOF-on-stdin, pipe-vs-pty, "just slow" -- 30s+ waits still
        # never respond). Without a timeout this call blocks forever;
        # with it, the probe still terminates and reports the finding.
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
            b1 = self.proc.stdout.read(1)
            if not b1:
                raise EOFError(self.proc.stderr.read().decode())
            header += b1
        length = int(header.split(b"Content-Length:")[1].split(b"\r\n")[0])
        return json.loads(self.proc.stdout.read(length))

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
        print(f"  Q1/Q4 BLOCKED: {e}")
        print(
            "  Confirmed separately: `ocamlearlybird debug` responds "
            "correctly to the same bytes when driven via a shell "
            "redirect/FIFO or Node's child_process.spawn, and `strace` "
            "shows its reader thread does read() the request -- but it "
            "never responds when spawned from Python (subprocess.Popen "
            "*and* asyncio.create_subprocess_exec, plain pipe or pty). "
            "This blocks Approach A (direct stdio spawn from tdb, a "
            "Python asyncio process) for the earlybird adapter as "
            "currently designed; see the spec's Probe-verified facts."
        )
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
