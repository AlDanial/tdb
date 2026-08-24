That log settles it, and it points the opposite way from where I'd have guessed. Let me look at the exit path in the adapter.This is the key. Let me verify the specific failure mechanism — Alpine's `/bin/ps` is BusyBox, not procps-ng.That log answers the question definitively, and it invalidates my earlier recommendation. **This is not a timeout-tuning problem.** Do not scale the timeouts — that would have hidden a real bug. Here's what's actually happening.

## The evidence

Three things in that output rule out slowness:

- **`1 failed, 1 passed in 6.05s`.** The entire session took six seconds, five of which were the doomed wait. Everything else was fast. Under CPU starvation you'd see pressure spread across every wait, not one clean stall.
- **The message list is complete and correct.** `continue` succeeded (seq 20), `continued` fired (seq 21), and `after library` — line 4, the last `echo` in `stepping.csh` — arrived (seq 22). The script then hits `exit 0` on line 5. The debuggee ran to completion, correctly.
- **`stderr=''`.** No tcsh error, no traceback. Silence.

So tcsh exited cleanly and the adapter simply never noticed. That's a hang, not a slow path, and it's deterministic.

## Root cause

`guardian.py` shells out to `ps` to figure out when the debuggee's session has drained. Two call sites:

```python
# _live_session_members
result = subprocess.run(("/bin/ps", "-axo", "pid=,pgid=,stat="), ...)
if result.returncode != 0:
    return None

# _process_state_forked
os.execl("/bin/ps", "ps", "-o", "stat=", "-p", str(process_id))
```

On Alpine, `/bin/ps` is BusyBox, not procps-ng. I ran both invocations against BusyBox to confirm:

```
$ busybox ps -axo 'pid=,pgid=,stat='
ps: invalid option -- 'x'
exit=1

$ busybox ps -o stat= -p 1
ps: invalid option -- 'p'
exit=1
```

BusyBox `ps` supports only `[-o COL1,COL2=HEADER] [-T]`. The `-a`, `-x`, and `-p` flags don't exist. Both calls exit non-zero, so `_live_session_members` returns `None`.

Now follow that `None` into the normal-exit path:

```python
def _session_has_other_live_members(session_id: int) -> bool:
    members = _live_session_members(session_id)
    if members is None:
        return True  # inspection failed -> "yes, still alive"
    return bool(members)
```

and:

```python
# _wait_for_drain_or_termination
while member_check(session_id):
    ...
```

`None` means "I couldn't tell," but it's coerced to "members are still alive," and that's the condition of an **unbounded loop**. The guardian spins forever, `process.wait()` never returns in `_monitor_process`, `_emit_process_termination` is never called, and no `exited` event is ever sent. Exactly what you see.

Note that the *terminate* path handles the same `None` correctly — it calls `_fail_termination`, which reports status and raises. Only the passive drain path fails open into an infinite loop. That's why `test_stop_on_entry_precedes_first_command` passed: it doesn't wait on natural process exit.

## Two separate defects

1. **Portability:** `/bin/ps` is assumed to be procps/BSD. Broken on Alpine and anything else BusyBox-based. This hits real users, not just CI — anyone running tdb in a slim container.
2. **Fail-open in an unbounded loop:** "I don't know" is treated as "keep waiting," with no deadline. This is the worse of the two, because it converts any inspection failure into a silent hang instead of a diagnosable error.

## What I'd do

**Fix the loop first, regardless of anything else.** `None` should be fatal, matching the terminate path:

```python
def _session_has_other_live_members(session_id: int) -> bool:
    members = _live_session_members(session_id)
    if members is None:
        raise _TerminationFailure("process inspection")
    return bool(members)
```

Catch it in `_wait_for_drain_or_termination`, report status, and return. A loud failure beats a hang even after the portability fix lands, since `ps` can fail for other reasons.

**Then drop the `ps` dependency on Linux** and read `/proc` directly. Fields 3, 5, and 6 after the `comm` field of `/proc/<pid>/stat` are state, pgrp, and session — everything the parser needs:

```python
def _live_session_members_via_proc(session_id, excluded_pids):
    members, guardian_pid = {}, os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == guardian_pid or pid in excluded_pids:
            continue
        try:
            data = open(f"/proc/{pid}/stat", "rb").read().decode("latin-1")
        except OSError:
            continue
        fields = data[data.rfind(")") + 2 :].split()  # comm may contain spaces/parens
        state, pgrp, sid = fields[0], int(fields[2]), int(fields[3])
        if state == "Z" or pgrp <= 0 or sid != session_id:
            continue
        members[pid] = pgrp
    return members
```

I ran this and cross-checked every returned pid against `os.getsid()` — it agrees. Two bonuses: it's identical across Alpine, Debian, and any Linux, and it removes a fork+exec per poll at a 0.05s polling interval, which is a lot of churn on the terminate path.

Keep the `ps` version as the non-Linux fallback (macOS BSD `ps` handles `-axo` fine, which is why your local runs are green). Guard on `sys.platform`, or just `os.path.isdir("/proc")`.

**If you want a portable single `ps` invocation** instead of the `/proc` route, use `-Ao pid,pgid,stat`. I verified BusyBox accepts `-A` and exits 0, and it's valid on procps and BSD too. The existing parser already skips BusyBox's header line, since `int("PID")` raises `ValueError` and hits the `continue`. Same fix applies to `_process_state_forked`, which needs to stop using `-p`.

**One Dockerfile gotcha:** `apk add procps` looks like a quick unblock but won't work — Alpine installs it at `/usr/bin/ps` and leaves `/bin/ps` pointing at BusyBox, and the guardian hardcodes `/bin/ps`. The code has to change either way.

**On my earlier advice:** the environment-scaling and `--init` suggestions are still reasonable hygiene, and moving pytest out of `docker build` is worth doing for the log visibility alone — that's what got you this traceback. But the 5-second timeouts were a red herring. They did their job here: they surfaced a genuine hang in about the right amount of time. Leave them where they are.
