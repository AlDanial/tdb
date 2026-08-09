"""Owns the instrumented bash subprocess and its two control pipes."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import re
import shutil
import signal
import tempfile
from typing import Callable

from tdb.adapters.bash.declares import BashVar, parse_declares

log = logging.getLogger(__name__)

HARNESS = os.path.join(os.path.dirname(__file__), "tdb_harness.sh")

# Large `globals`/`eval`/`locals` payloads (base64-encoded) can comfortably
# exceed asyncio.StreamReader's default 64KiB readline limit; give the
# harness response channel plenty of headroom.
_RESP_LIMIT = 8 * 1024 * 1024


class BashProtocolError(Exception):
    pass


def canonical(path: str, base: str | None = None) -> str:
    """MUST match the harness: realpath of the directory + raw basename.

    `base` is the debuggee's launch cwd (BashSession.launch_cwd) — relative
    `path`s are joined against it, matching how the harness resolves
    relative source paths against its own (the debuggee's) cwd. Falls back
    to the current process's cwd when `base` is omitted, for callers that
    don't have a session handy (e.g. plain unit tests).
    """
    if not os.path.isabs(path):
        path = os.path.join(base or os.getcwd(), path)
    return os.path.join(
        os.path.realpath(os.path.dirname(path)),
        os.path.basename(path),
    )


def b64(value: str) -> str:
    if not value:
        return "-"
    return base64.b64encode(value.encode()).decode()


def unb64(field: str) -> str:
    if field == "-":
        return ""
    return base64.b64decode(field).decode(errors="replace")


# variables the debuggee didn't create: bash specials + harness state.
# BASH_REMATCH/PIPESTATUS etc. change under the harness's own feet, so
# showing them would mislead; users can still `eval echo $PIPESTATUS`.
#
# BASH and COMP_ are kept as unanchored prefixes — those namespaces are
# bash-owned in practice (BASH_*, BASHPID, BASHOPTS, COMP_*) — but every
# other entry is anchored/enumerated to the exact bash special it names.
# An unanchored prefix here silently hides real user variables (HISTORY,
# EPOCH_START, SHELLCHECK_OPTS all collided with the old HIST/EPOCH/SHELL
# prefixes).
_INTERNAL_VARS = re.compile(
    r"^(__tdb_|__TDB_|BASH|SHELL$|SHELLOPTS$|IFS$|PS4$|"
    r"EPOCHREALTIME$|EPOCHSECONDS$|EUID$|UID$|PPID$|RANDOM$|"
    r"SECONDS$|SRANDOM$|LINENO$|FUNCNAME$|GROUPS$|DIRSTACK$|PIPESTATUS$|"
    r"COMP_|COMPREPLY$|FUNCNEST$|OPTARG$|"
    r"HIST(CMD|CONTROL|FILE|FILESIZE|IGNORE|SIZE|TIMEFORMAT)$|"
    r"HOSTNAME$|HOSTTYPE$|MACHTYPE$|OSTYPE$|OLDPWD$|OPTERR$|"
    r"OPTIND$|PATH$|PWD$|SHLVL$|TERM$|_$)"
)


class BashSession:
    def __init__(
        self,
        on_output: Callable[[str, str], None],
        on_stop: Callable[[str, str, int], None],
        on_exit: Callable[[int], None],
    ) -> None:
        self._on_output = on_output
        self._on_stop = on_stop
        self._on_exit = on_exit
        self.stopped = False
        self.exit_code: int | None = None
        self.launch_cwd: str | None = None  # for canonical(path, base=launch_cwd)
        self._process: asyncio.subprocess.Process | None = None
        self._cmd_w: int | None = None
        self._resp_reader: asyncio.StreamReader | None = None
        self._pending: asyncio.Future | None = None  # one in-flight ok/err request
        self._pending_id: int | None = None  # correlation id for _pending (I1)
        self._req_id = 0  # monotonic REQUEST id counter
        self._ready: asyncio.Future | None = None
        self._tasks: list[asyncio.Task] = []
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self._stderr_tail = ""  # last stderr bytes, for pre-ready failures

    async def launch(
        self,
        *,
        program: str,
        args: list[str],
        cwd: str,
        env: dict | None,
        bash: str = "bash",
    ) -> None:
        if os.name == "nt":
            raise BashProtocolError(
                "bash debugging is not supported on native Windows in this "
                "version — run tdb inside WSL instead"
            )
        bash_path = shutil.which(bash)
        if bash_path is None:
            raise BashProtocolError(
                f"bash not found ({bash!r}) — install bash >= 4.4 or set "
                '{"adapters": {"bash": "/path/to/bash"}} in tdb\'s config.json'
            )
        if not os.path.isfile(HARNESS):
            raise BashProtocolError(
                f"tdb's bash harness is missing from this installation "
                f"({HARNESS}) — the tdb package was built without its "
                f"package-data; reinstall tdb"
            )
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self.launch_cwd = os.path.abspath(cwd)
        self._tmpdir = tempfile.TemporaryDirectory(prefix="tdb-bash-")
        cmd_r, cmd_w = os.pipe()  # adapter writes, bash reads
        resp_r, resp_w = os.pipe()  # bash writes, adapter reads
        os.set_inheritable(cmd_r, True)
        os.set_inheritable(resp_w, True)
        child_env = dict(env or os.environ)
        child_env["BASH_ENV"] = HARNESS
        child_env["__TDB_CMD_FD"] = str(cmd_r)
        child_env["__TDB_RESP_FD"] = str(resp_w)
        child_env["__TDB_TMP"] = self._tmpdir.name
        self._process = await asyncio.create_subprocess_exec(
            bash_path,
            program,
            *args,
            cwd=cwd,
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            pass_fds=(cmd_r, resp_w),
        )
        os.close(cmd_r)
        os.close(resp_w)
        self._cmd_w = cmd_w
        self._resp_reader = asyncio.StreamReader(limit=_RESP_LIMIT)
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self._resp_reader),
            os.fdopen(resp_r, "rb"),
        )
        self._tasks = [
            asyncio.create_task(self._resp_loop()),
            asyncio.create_task(self._pump(self._process.stdout, "stdout")),
            asyncio.create_task(self._pump(self._process.stderr, "stderr")),
            asyncio.create_task(self._reap()),
        ]
        try:
            await asyncio.wait_for(self._ready, 15.0)
        except (asyncio.TimeoutError, BashProtocolError) as e:
            await self.stop()
            if isinstance(e, BashProtocolError):
                raise
            raise BashProtocolError(
                "the bash harness never reported ready — is bash hung during startup?"
            )
        self.stopped = True  # config phase

    async def _pump(self, stream: asyncio.StreamReader, category: str) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            text = chunk.decode(errors="replace")
            if category == "stderr":
                self._stderr_tail = (self._stderr_tail + text)[-500:]
            self._on_output(text, category)

    async def _reap(self) -> None:
        # asyncio.subprocess.Process.wait() does NOT resolve on process exit
        # alone: internally (base_subprocess._try_finish) it's also gated on
        # every stdio pipe transport reporting "disconnected". A backgrounded
        # grandchild (`cmd &`) that inherited stdout/stderr keeps those
        # pipes' write ends open long after the direct child (bash) has
        # actually exited, which hangs wait() — and therefore on_exit —
        # indefinitely. `Process.returncode` is set by asyncio's own
        # child-watcher callback (_process_exited) the moment the OS reports
        # the direct child's exit, independent of pipe state, so fall back
        # to polling that (still fully watcher-driven, no competing
        # os.waitpid) if wait() doesn't resolve promptly.
        try:
            code = await asyncio.wait_for(self._process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            while self._process.returncode is None:
                await asyncio.sleep(0.02)
            code = self._process.returncode
        self.exit_code = code
        # Let the output pumps flush whatever's already buffered, but don't
        # block on their EOF for the same reason. Bounded wait, then move
        # on — the pump tasks get cancelled here (via wait_for's
        # timeout->cancel, which asyncio.gather propagates to its children)
        # or later by stop(); any output written by a lingering grandchild
        # after that point is not captured, which matches "the debug
        # session ended".
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks[1:3], return_exceptions=True),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            pass
        if self._ready and not self._ready.done():
            # died before the handshake: fail launch() fast with the real
            # reason (e.g. the harness's "bash >= 4.4 required" line)
            tail = self._stderr_tail.strip()
            self._ready.set_exception(
                BashProtocolError(
                    "bash exited before the harness reported ready"
                    + (f": {tail}" if tail else " (bash >= 4.4 required?)")
                )
            )
            return
        if self._pending and not self._pending.done():
            self._pending.set_exception(BashProtocolError("debuggee exited"))
        self._on_exit(code)

    async def _resp_loop(self) -> None:
        while True:
            line = await self._resp_reader.readline()
            if not line:
                return
            try:
                fields = line.decode(errors="replace").split()
                if not fields:
                    continue
                kind = fields[0]
                if kind == "ready":
                    if self._ready and not self._ready.done():
                        self._ready.set_result(None)
                elif kind == "stopped":
                    self.stopped = True
                    self._on_stop(fields[1], unb64(fields[2]), int(fields[3]))
                elif kind in ("ok", "err"):
                    # I1: the harness echoes back the id of the REQUEST it's
                    # replying to ("ok 7 <b64>" / "err 7 <b64>"; "-" for a
                    # bare/fire-and-forget command). A reply whose id
                    # doesn't match what we're currently waiting on is a
                    # late/stale reply for a request we already timed out
                    # on -- drop it instead of resolving whatever request
                    # happens to be pending now (that used to shift every
                    # later reply by one).
                    reply_id = fields[1] if len(fields) > 1 else "-"
                    if self._pending is None or reply_id != str(self._pending_id):
                        continue
                    payload = fields[2] if len(fields) > 2 else "-"
                    fut, self._pending = self._pending, None
                    self._pending_id = None
                    if fut and not fut.done():
                        if kind == "ok":
                            fut.set_result(unb64(payload))
                        else:
                            fut.set_exception(BashProtocolError(unb64(payload)))
            except (IndexError, ValueError, binascii.Error) as e:
                # A malformed frame must not kill this task — that would
                # silently wedge the whole session (no more stop/ok/err
                # notifications ever arrive again).
                log.warning("malformed harness frame %r: %s", line, e)
                continue

    def _write_line(self, line: str) -> None:
        if self._cmd_w is None:
            raise BashProtocolError("no session")
        try:
            os.write(self._cmd_w, (line + "\n").encode())
        except OSError as e:
            raise BashProtocolError(f"failed to write to bash harness: {e}") from e

    def send_async(self, line: str) -> None:
        """Fire-and-forget while the debuggee is running (bp edits, pause)."""
        self._write_line(line)

    async def request(self, line: str, timeout: float = 15.0) -> str:
        """Send a command that gets an ok/err reply (stopped/config phase only).

        The line is sent prefixed with a monotonic correlation id (I1) so a
        late reply for a request this call already timed out on can never
        be mistaken for the answer to a different, later request.
        """
        if self._pending is not None:
            raise BashProtocolError("another request is in flight")
        self._req_id += 1
        req_id = self._req_id
        self._pending = asyncio.get_running_loop().create_future()
        self._pending_id = req_id
        self._write_line(f"{req_id} {line}")
        try:
            return await asyncio.wait_for(self._pending, timeout)
        finally:
            self._pending = None
            self._pending_id = None

    def resume(self, mode: str) -> None:
        if mode not in ("step", "next", "finish", "continue"):
            raise ValueError(f"invalid resume mode: {mode!r}")
        self.stopped = False
        self._write_line(mode)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._process is not None:
            # Always try to kill the whole process group, even if bash
            # itself has already exited (self._process.returncode is set):
            # _reap() can observe bash's exit — via polling
            # Process.returncode, see _reap()'s comment — well before a
            # backgrounded grandchild (`cmd &`) that's still alive in the
            # same process group has been reaped, and skipping this here
            # would leak it. launch() uses start_new_session=True (setsid),
            # so the pgid equals bash's own pid by construction; use that
            # directly rather than os.getpgid(pid), which raises
            # ProcessLookupError once bash's pid has already been reaped.
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            if self._process.returncode is None:
                await self._process.wait()
        if self._cmd_w is not None:
            try:
                os.close(self._cmd_w)
            except OSError:
                pass
            self._cmd_w = None
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    def _bp_line(self, path: str, line: int, condition: str) -> str:
        return f"setbp {b64(canonical(path, self.launch_cwd))} {line} {b64(condition)}"

    async def set_breakpoint(self, path: str, line: int, condition: str = "") -> None:
        await self.request(self._bp_line(path, line, condition))

    def set_breakpoint_nowait(self, path: str, line: int, condition: str = "") -> None:
        self.send_async(self._bp_line(path, line, condition))

    async def clear_breakpoints(self) -> None:
        await self.request("clearall")

    def clear_breakpoints_nowait(self) -> None:
        self.send_async("clearall")

    def pause(self) -> None:
        self.send_async("pause")

    async def stack(self) -> list[dict]:
        payload = await self.request("stack")
        frames = []
        for line in payload.splitlines():
            func, file, lineno = line.rsplit("|", 2)
            frames.append({"func": func, "file": file, "line": int(lineno)})
        return frames

    async def locals(self) -> list[BashVar]:
        return parse_declares(await self.request("locals"))

    async def globals_vars(self) -> list[BashVar]:
        """Unexported shell variables (the script's own state).

        Exported variables — inherited environment and the script's own
        exports alike — live in environment_vars() instead; the strict
        split means nothing appears in both.
        """
        return [
            v
            for v in parse_declares(await self.request("globals"))
            if not v.exported and not _INTERNAL_VARS.match(v.name)
        ]

    async def environment_vars(self) -> list[BashVar]:
        """Exported variables (bash's actual environment).

        Deliberately NOT filtered by _INTERNAL_VARS — PATH/HOME/PWD/TERM
        are the point of an environment tree. Only the harness's own
        control variables are hidden.
        """
        return [
            v
            for v in parse_declares(await self.request("globals"))
            if v.exported and not v.name.startswith(("__tdb_", "__TDB_"))
        ]

    async def evaluate(self, expr: str) -> tuple[int, str]:
        payload = await self.request("eval " + b64(expr))
        first, _, rest = payload.partition("\n")
        rc = int(first.removeprefix("rc=") or 0)
        return rc, rest
