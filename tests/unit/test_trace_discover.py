"""``tdb --trace-discover``: count parts -> spec YAML, and launch's discover
branch, with hand-made parts (no traced program is started)."""

from __future__ import annotations

import json
import os
import re
import signal
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tdb.trace import discover, launch
from tdb.trace.discover import Discovered, write_discovered_spec
from tdb.trace.launch import LaunchError, TraceRequest, run
from tdb.trace.spec import load_spec, norm_path

DEAD_PID = 1 << 30  # beyond any pid_max, so never a live process
STARTED = datetime(2026, 11, 25, 10, 42, 5)
HDR = {"k": "hdr", "pid": DEAD_PID, "ppid": 1, "proc": "MainProcess"}
HDR.update(argv=["x"], py="3.12.8", wall=1, pc=0, mode="discover")
EXIT = {"k": "exit", "t": 9, "how": "atexit"}


def count(file, qual, line, n, kind="function", capped=False) -> dict:
    return {
        "k": "count",
        "file": norm_path(file),
        "qual": qual,
        "line": line,
        "kind": kind,
        "n": n,
        "capped": capped,
    }


def part(path: Path, *records: dict, tail: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
    path.write_text(text + tail, encoding="utf-8")


def source(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write(parts_dir: Path, out: Path, **kw) -> Discovered:
    kw.setdefault("program_desc", "prog.py")
    kw.setdefault("started", STARTED)
    kw.setdefault("duration_s", 3.0)
    return write_discovered_spec(parts_dir, out, **kw)


def body(out: Path) -> str:
    """OUT.yaml without its two header lines and the blank line after them."""
    lines = out.read_text(encoding="utf-8").split("\n")
    assert lines[1] == "# Remove what you do not need; hot functions make traces large."
    assert lines[2] == ""
    return "\n".join(lines[3:])


API = """\
class Api:
    async def checkout(self):
        pass

    def _validate(self):
        pass


def parse_header(line):
    pass
"""


def test_spec_text_from_count_parts(tmp_path):
    api = source(tmp_path / "api.py", API)
    svc = source(tmp_path / "my svc.py", "def handle():\n    pass\n")
    parts = tmp_path / "out.yaml.parts"
    # Counts are cumulative within a part (the largest wins) and are
    # summed over the parts (processes).
    part(
        parts / f"{DEAD_PID}.jsonl",
        HDR,
        count(api, "Api.checkout", 2, 500, "coroutine"),
        count(api, "Api._validate", 5, 1),
        count(api, "Api.checkout", 2, 704, "coroutine"),
        count(api, "parse_header", 9, 10_000, capped=True),
        EXIT,
    )
    part(
        parts / f"{DEAD_PID + 1}.jsonl",
        {**HDR, "pid": DEAD_PID + 1},
        count(api, "Api.checkout", 2, 500, "coroutine"),
        count(svc, "handle", 1, 2),
        EXIT,
    )
    (parts / "run.json").write_text("{}")
    (parts / "config.json").write_text("{}")
    out = tmp_path / "out.yaml"
    n = write(
        parts,
        out,
        program_desc="services/orders/main.py",
        duration_s=192.4,
    )
    assert n == Discovered(functions=4, omitted=0)
    assert out.read_text(encoding="utf-8") == (
        "# tdb --trace-discover: services/orders/main.py, 2026-11-25 10:42, 3m12s\n"
        "# Remove what you do not need; hot functions make traces large.\n"
        "\n"
        "api.py:\n"
        "  - Api::checkout   # 1,204 calls, async\n"
        "  - Api::_validate  # 1 call\n"
        "  - parse_header    # ≥10,000 calls  (hot)\n"
        "\n"
        '"my svc.py":\n'
        "  - handle  # 2 calls\n"
    )
    spec = load_spec(out)
    assert [(e.key, [f.pattern for f in e.functions]) for e in spec.entries] == [
        ("api.py", ["Api.checkout", "Api._validate", "parse_header"]),
        ("my svc.py", ["handle"]),
    ]
    assert spec.entries[0].files == (norm_path(api),)


def test_capped_when_any_part_capped_it_or_the_sum_reaches_the_cap(tmp_path):
    prog = source(
        tmp_path / "prog.py",
        "def f():\n    pass\ndef g():\n    pass\n"
        "def h():\n    pass\nasync def k():\n    yield\n",
    )
    parts = tmp_path / "parts"
    part(
        parts / "1.jsonl",
        HDR,
        count(prog, "f", 1, 6),
        count(prog, "g", 3, 10, capped=True),
        count(prog, "h", 5, 4),
        count(prog, "k", 7, 1, "async_generator"),
    )
    part(
        parts / "2.jsonl",
        HDR,
        count(prog, "f", 1, 5),
        count(prog, "g", 3, 1),
        count(prog, "h", 5, 5),
    )
    out = tmp_path / "out.yaml"
    assert write(parts, out, cap=10) == (4, 0)
    assert body(out) == (
        "prog.py:\n"
        "  - f  # ≥10 calls  (hot)\n"
        "  - g  # ≥10 calls  (hot)\n"
        "  - h  # 9 calls\n"
        "  - k  # 1 call, async\n"
    )


@pytest.mark.parametrize(
    "seconds,text",
    [(0.0, "0s"), (0.4, "0s"), (45.2, "45s"), (59.6, "1m00s"), (192, "3m12s")],
)
def test_duration(seconds, text):
    assert discover.format_duration(seconds) == text


def test_file_keys_relative_to_the_spec_or_absolute(tmp_path):
    d = tmp_path / "a" / "b" / "c" / "d"
    files = {
        "here": source(d / "x.py", "def f():\n    pass\n"),
        "sub": source(d / "sub" / "s.py", "def f():\n    pass\n"),
        "up1": source(tmp_path / "a" / "b" / "c" / "y.py", "def f():\n    pass\n"),
        "up3": source(tmp_path / "a" / "z.py", "def f():\n    pass\n"),
        "up4": source(tmp_path / "w.py", "def f():\n    pass\n"),
    }
    parts = tmp_path / "parts"
    part(parts / "1.jsonl", HDR, *(count(p, "f", 1, 1) for p in files.values()))
    out = d / "out.yaml"
    assert write(parts, out) == (5, 0)
    absolute = norm_path(files["up4"]).replace(os.sep, "/")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", absolute):
        absolute = json.dumps(absolute)
    keys = [line[:-1] for line in body(out).split("\n") if line.endswith(":")]
    assert keys == sorted(["x.py", "sub/s.py", "../y.py", "../../../z.py", absolute])
    spec = load_spec(out)
    assert sorted(f for e in spec.entries for f in e.files) == sorted(
        norm_path(p) for p in files.values()
    )


def test_names_and_keys_yaml_would_misread_are_quoted(tmp_path):
    """Everything written loads back as the same strings: YAML words
    (``on``, ``null``), a file load_spec would take for a module name, glob
    characters, and characters beyond U+FFFF."""
    events = source(
        tmp_path / "events.py",
        "def on():\n    pass\n\ndef null():\n    pass\n\n"
        "def outer():\n    def inner():\n        pass\n",
    )
    tool = source(tmp_path / "tool", "def main():\n    pass\n")
    bracket = source(tmp_path / "a[1].py", "def f():\n    pass\n")
    try:
        wide = source(tmp_path / "\u00fc\U0001f600.py", "def f():\n    pass\n")
    except (OSError, UnicodeError):  # a file system that refuses the name
        pytest.skip("cannot create a file with a non-BMP name")
    parts = tmp_path / "parts"
    part(
        parts / "1.jsonl",
        HDR,
        count(events, "on", 1, 1),
        count(events, "null", 4, 1),
        count(events, "outer.inner", 8, 1),
        count(tool, "main", 1, 1),
        count(bracket, "f", 1, 1),
        count(wide, "f", 1, 1),
    )
    out = tmp_path / "out.yaml"
    assert write(parts, out) == (6, 0)
    # The width counts the quotes too.
    assert body(out) == (
        "./tool:\n"
        "  - main  # 1 call\n"
        "\n"
        '"a[[]1].py":\n'
        "  - f  # 1 call\n"
        "\n"
        "events.py:\n"
        '  - "on"          # 1 call\n'
        '  - "null"        # 1 call\n'
        "  - outer::inner  # 1 call\n"
        "\n"
        '"\u00fc\U0001f600.py":\n'  # raw UTF-8: a short key
        "  - f  # 1 call\n"
    )
    spec = load_spec(out)
    by_file = {
        f: [x.pattern for x in e.functions] for e in spec.entries for f in e.files
    }
    assert by_file == {
        norm_path(events): ["on", "null", "outer.inner"],
        norm_path(tool): ["main"],
        norm_path(bracket): ["f"],
        norm_path(wide): ["f"],
    }


def test_header_escapes_what_a_yaml_comment_cannot_hold(tmp_path):
    prog = source(tmp_path / "prog.py", "def f():\n    pass\n")
    parts = tmp_path / "parts"
    part(parts / "1.jsonl", HDR, count(prog, "f", 1, 1))
    out = tmp_path / "out.yaml"
    write(parts, out, program_desc="bad\nname\udcff.py")
    first = out.read_text(encoding="utf-8").split("\n")[0]
    assert first == "# tdb --trace-discover: bad\\nname\\udcff.py, 2026-11-25 10:42, 3s"
    assert [e.key for e in load_spec(out).entries] == ["prog.py"]


def test_unreadable_lines_and_other_records_are_ignored(tmp_path):
    prog = source(tmp_path / "prog.py", "def f():\n    pass\ndef g():\n    pass\n")
    parts = tmp_path / "parts"
    bad = count(prog, "g", 3, 7)
    part(
        parts / "1.jsonl",
        HDR,
        {"k": "proc", "t": 1, "name": "worker"},
        count(prog, "f", 1, 2),
        {**bad, "n": True},  # not a count
        {**bad, "line": "3"},
        {**bad, "qual": None},
        ["not", "a", "record"],
        EXIT,
        tail='{"k":"count","file":',  # cut off by a kill
    )
    part(parts / "2.jsonl")  # empty: killed before its header
    part(parts / "skipped-5.json", {"marker": "skipped"})
    part(parts / "notes.jsonl", count(prog, "g", 3, 7))  # not a part file name
    out = tmp_path / "out.yaml"
    assert write(parts, out) == (1, 0)
    assert body(out) == "prog.py:\n  - f  # 2 calls\n"


def test_nothing_counted_writes_only_the_header(tmp_path):
    (tmp_path / "parts").mkdir()
    out = tmp_path / "out.yaml"
    assert write(tmp_path / "parts", out) == (0, 0)
    assert body(out) == ""


def test_spec_written_whole_or_not_at_all(tmp_path):
    prog = source(tmp_path / "prog.py", "def f():\n    pass\n")
    parts = tmp_path / "parts"
    part(parts / "1.jsonl", HDR, count(prog, "f", 1, 1))
    out = tmp_path / "out.yaml"
    out.mkdir()  # os.replace onto it fails
    with pytest.raises(OSError):
        write(parts, out)
    assert sorted(os.listdir(tmp_path)) == ["out.yaml", "parts", "prog.py"]
    assert os.listdir(out) == []


# --- launch.run in discover mode (the program is a stand-in) ---------------


def request(tmp_path: Path, **kw) -> TraceRequest:
    # The command form: no interpreter probe, which tdb's own Python (3.11
    # is allowed for tdb) would fail.
    fields = dict(
        mode="discover",
        program=sys.executable,
        module=None,
        args=["-c", "pass"],
        python=None,
        cwd=None,
        name="n",
        out_base=tmp_path / "unused",
        discover_out=tmp_path / "out.yaml",
    )
    fields.update(kw)
    return TraceRequest(**fields)


def fake_run(monkeypatch, tmp_path: Path, returncode: int = 0, leave=None) -> dict:
    """Make the program a stand-in that leaves one part counting prog.f, or
    whatever ``leave(parts_dir, prog)`` writes; returns what it saw of the
    parts directory."""
    prog = source(tmp_path / "prog.py", "def f():\n    pass\n")
    seen: dict = {}

    def one_part(parts_dir, prog):
        part(parts_dir / f"{DEAD_PID}.jsonl", HDR, count(prog, "f", 1, 3), EXIT)

    class Ended:
        pid = DEAD_PID

    Ended.returncode = returncode

    def supervised(cmd, req, parts_dir):
        seen["parts_dir"] = parts_dir
        seen["config"] = json.loads((parts_dir / "config.json").read_text())
        (leave or one_part)(parts_dir, prog)
        return Ended()

    monkeypatch.setattr(launch, "_supervise", supervised)
    return seen


@pytest.mark.parametrize(
    "returncode", [0, 3, -signal.SIGTERM], ids=["ok", "exit-3", "SIGTERM"]
)
def test_run_writes_the_spec_and_removes_the_parts(
    tmp_path, monkeypatch, capsys, returncode
):
    seen = fake_run(monkeypatch, tmp_path, returncode)
    out = tmp_path / "out.yaml"
    # Ruling 24, as for --trace: the exit code, or -N to die by signal N.
    assert run(request(tmp_path)) == launch._end_status(returncode)
    assert seen["parts_dir"] == tmp_path / "out.yaml.parts"
    assert (seen["config"]["mode"], seen["config"]["targets"]) == ("discover", [])
    assert capsys.readouterr().err == f"tdb: wrote {out} (1 function)\n"
    assert sorted(os.listdir(tmp_path)) == ["out.yaml", "prog.py"]
    lines = out.read_text(encoding="utf-8").split("\n")
    assert re.fullmatch(
        rf"# tdb --trace-discover: {re.escape(sys.executable)}, "
        r"\d{4}-\d\d-\d\d \d\d:\d\d, \d+s",
        lines[0],
    ), lines[0]
    assert lines[3:] == ["prog.py:", "  - f  # 3 calls", ""]


def test_run_describes_a_module_as_dash_m(tmp_path, monkeypatch, capsys):
    fake_run(monkeypatch, tmp_path)
    monkeypatch.setattr(launch, "probe_version", lambda interpreter: (3, 12))
    req = request(tmp_path, program=None, module="orders.main", args=[])
    assert run(req) == 0
    first = (tmp_path / "out.yaml").read_text(encoding="utf-8").split("\n")[0]
    assert first.startswith("# tdb --trace-discover: -m orders.main, ")


def test_failed_write_keeps_the_parts(tmp_path, monkeypatch, capsys):
    fake_run(monkeypatch, tmp_path)
    out = tmp_path / "out.yaml"
    out.mkdir()  # os.replace onto it fails
    assert run(request(tmp_path)) == 1
    parts = tmp_path / "out.yaml.parts"
    (line,) = capsys.readouterr().err.splitlines()
    assert line.startswith(f"tdb: error: could not write {out}: ")
    assert line.endswith(f"; events kept in {parts}")
    assert sorted(os.listdir(tmp_path)) == ["out.yaml", "out.yaml.parts", "prog.py"]
    assert f"{DEAD_PID}.jsonl" in os.listdir(parts) and os.listdir(out) == []


def test_ctrl_c_while_writing_keeps_the_parts(tmp_path, monkeypatch, capsys):
    fake_run(monkeypatch, tmp_path)

    def interrupted(*args, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(discover, "write_spec", interrupted)
    assert run(request(tmp_path)) == launch._INTERRUPTED
    parts = tmp_path / "out.yaml.parts"
    assert capsys.readouterr().err == f"tdb: interrupted; events kept in {parts}\n"
    assert sorted(os.listdir(tmp_path)) == ["out.yaml.parts", "prog.py"]


@pytest.mark.parametrize(
    "mode,flag", [("discover", "--trace-discover"), ("trace", "--trace")]
)
def test_version_message_names_the_mode(tmp_path, monkeypatch, mode, flag):
    monkeypatch.setattr(launch, "probe_version", lambda interpreter: (3, 11))
    req = request(tmp_path, mode=mode, program=None, module="m", args=[])
    with pytest.raises(
        LaunchError, match=rf"^{flag} needs Python 3\.12 or newer; .* is 3\.11$"
    ):
        run(req)
    assert os.listdir(tmp_path) == []


def test_existing_parts_dir_refused(tmp_path):
    (tmp_path / "out.yaml.parts").mkdir()
    with pytest.raises(
        LaunchError,
        match=r"out\.yaml\.parts already exists \(left by an earlier run\?\); "
        r"remove it$",
    ):
        run(request(tmp_path))
    assert os.listdir(tmp_path) == ["out.yaml.parts"]


# --- fix round 1 ------------------------------------------------------------


def test_only_functions_their_source_defines_are_kept(tmp_path):
    """Ruling 27: what load_spec would reject is left out, and counted:
    names the file does not define (3.14's __annotate__, 3.12's ``type X =
    ...`` value), a file that is no Python (a template compiled under its
    own path), files that do not exist (a zipapp's member, a deleted
    file)."""
    prog = source(
        tmp_path / "prog.py", "class Point:\n    def norm(self):\n        pass\n"
    )
    template = source(tmp_path / "templates" / "index.html", "{% for x in xs %}\n")
    (tmp_path / "app.pyz").write_bytes(b"PK\x05\x06" + bytes(18))
    parts = tmp_path / "parts"
    part(
        parts / "1.jsonl",
        HDR,
        count(prog, "Point.norm", 2, 3),
        count(prog, "Point.__annotate__", 1, 1),
        count(prog, "Alias", 4, 1),
        count(template, "root", 1, 2),
        count(tmp_path / "app.pyz" / "__main__.py", "main", 1, 1),
        count(tmp_path / "gone.py", "f", 1, 1),
    )
    out = tmp_path / "out.yaml"
    assert write(parts, out) == Discovered(functions=1, omitted=5)
    assert body(out) == "prog.py:\n  - Point::norm  # 3 calls\n"
    assert [e.key for e in load_spec(out).entries] == ["prog.py"]


def test_one_entry_per_name(tmp_path):
    """M8: a property's getter and setter, or a def in each branch of an
    if, are one name to load_spec: one entry, their counts summed."""
    prog = source(
        tmp_path / "prog.py",
        "class C:\n    @property\n    def v(self):\n        return 1\n\n"
        "    @v.setter\n    def v(self, value):\n        pass\n\n"
        "if True:\n    def g():\n        pass\n"
        "else:\n    async def g():\n        pass\n",
    )
    parts = tmp_path / "parts"
    part(
        parts / "1.jsonl",
        HDR,
        count(prog, "C.v", 2, 10_000, capped=True),
        count(prog, "C.v", 6, 1),
        count(prog, "g", 11, 2),
        count(prog, "g", 14, 1, "coroutine"),
    )
    out = tmp_path / "out.yaml"
    assert write(parts, out) == (2, 0)
    assert body(out) == (
        "prog.py:\n  - C::v  # ≥10,000 calls  (hot)\n  - g     # 3 calls, async\n"
    )
    assert [f.pattern for f in load_spec(out).entries[0].functions] == ["C.v", "g"]


def test_long_and_non_ascii_keys_load(tmp_path):
    """M5: PyYAML reads no simple key longer than 1024 characters.  Non-ASCII
    characters are written as they are (six characters each as \\u escapes:
    200 Cyrillic ones were too many), and a longer key takes the explicit
    ``? key`` form."""
    cyrillic = Path("п" * 100) / ("ф" * 96) / "м.py"
    deep = Path(*["d" * 200] * 6) / "x.py"
    try:
        files = [
            source(tmp_path / cyrillic, "def f():\n    pass\n"),
            source(tmp_path / deep, "def f():\n    pass\n"),
        ]
    except OSError:  # a short PATH_MAX (macOS, Windows)
        pytest.skip("cannot create a path this long here")
    parts = tmp_path / "parts"
    part(parts / "1.jsonl", HDR, *(count(f, "f", 1, 1) for f in files))
    out = tmp_path / "out.yaml"
    assert write(parts, out) == (2, 0)
    text = body(out)
    assert f'"{cyrillic.as_posix()}":\n  - f  # 1 call\n' in text
    assert f"? {deep.as_posix()}\n:\n  - f  # 1 call\n" in text
    spec = load_spec(out)
    assert sorted(f for e in spec.entries for f in e.files) == sorted(
        norm_path(f) for f in files
    )


def test_run_warns_when_no_process_was_traced(tmp_path, monkeypatch, capsys):
    """Issue 1: an empty result says why, as --trace does."""

    def skipped_only(parts_dir, prog):
        marker = {"marker": "skipped", "pid": DEAD_PID, "argv": ["/opt/py311"]}
        marker.update(python="3.11.9", reason="python 3.11 is older than 3.12")
        (parts_dir / f"skipped-{DEAD_PID}.json").write_text(json.dumps(marker))

    fake_run(monkeypatch, tmp_path, leave=skipped_only)
    out = tmp_path / "out.yaml"
    assert run(request(tmp_path)) == 0
    assert capsys.readouterr().err.splitlines() == [
        f"tdb: wrote {out} (0 functions)",
        f"tdb: warning: skipped /opt/py311 (pid {DEAD_PID}): "
        "python 3.11 is older than 3.12",
        launch.NO_PROCESS_WARNING,
    ]


def test_run_warns_about_tracer_errors_and_abrupt_ends(tmp_path, monkeypatch, capsys):
    """Issue 1: counts that may be incomplete are reported."""

    def damaged(parts_dir, prog):
        part(parts_dir / f"{DEAD_PID}.jsonl", HDR, count(prog, "f", 1, 3), EXIT)
        child = {**HDR, "pid": DEAD_PID + 1, "ppid": DEAD_PID}
        part(parts_dir / f"{DEAD_PID + 1}.jsonl", child, count(prog, "f", 1, 2))
        marker = {"marker": "agent-error", "pid": DEAD_PID + 2, "argv": ["x"]}
        marker["error"] = "cannot write the part file: disk full"
        (parts_dir / f"agent-error-{DEAD_PID + 2}.json").write_text(json.dumps(marker))

    fake_run(monkeypatch, tmp_path, leave=damaged)
    out = tmp_path / "out.yaml"
    assert run(request(tmp_path)) == 0
    assert capsys.readouterr().err.splitlines() == [
        f"tdb: wrote {out} (1 function)",
        f"tdb: warning: tracer error in pid {DEAD_PID + 2}: "
        "cannot write the part file: disk full",
        f"tdb: warning: pid {DEAD_PID + 1} ended abruptly; "
        "its last events may be missing",
    ]
    assert body(out) == "prog.py:\n  - f  # 5 calls\n"


def test_run_notes_the_functions_left_out(tmp_path, monkeypatch, capsys):
    def with_ghosts(parts_dir, prog):
        recs = [count(prog, "f", 1, 3), count(prog, "ghost", 5, 1)]
        recs.append(count(prog, "phantom", 9, 1))
        part(parts_dir / f"{DEAD_PID}.jsonl", HDR, *recs, EXIT)

    fake_run(monkeypatch, tmp_path, leave=with_ghosts)
    out = tmp_path / "out.yaml"
    assert run(request(tmp_path)) == 0
    assert capsys.readouterr().err.splitlines() == [
        f"tdb: wrote {out} (1 function)",
        "tdb: note: 2 functions omitted (not found in their source files)",
    ]


def test_unreadable_parts_are_reported_as_unreadable(tmp_path, monkeypatch, capsys):
    """M7: a read error is no write error."""
    fake_run(monkeypatch, tmp_path)

    def unreadable(parts_dir):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(discover, "read_counts", unreadable)
    assert run(request(tmp_path)) == 1
    parts = tmp_path / "out.yaml.parts"
    assert capsys.readouterr().err == (
        f"tdb: error: cannot read {parts}: [Errno 13] Permission denied\n"
    )
    assert sorted(os.listdir(tmp_path)) == ["out.yaml.parts", "prog.py"]


def test_user_roots_in_the_config(tmp_path, monkeypatch):
    """Ruling 26: the program's file and directory, so a program installed
    in site-packages is still the user's code; a command's file only if it
    is a Python script; none for -m."""
    seen = fake_run(monkeypatch, tmp_path)
    monkeypatch.setattr(launch, "probe_version", lambda interpreter: (3, 12))
    prog = tmp_path / "prog.py"
    script = tmp_path / "tool"
    script.write_text("#!/usr/bin/env python3\nprint('hi')\n")
    for req, roots in (
        (request(tmp_path, program=str(prog), args=[]), [prog, tmp_path]),
        (request(tmp_path, program=str(script)), [script]),
        (request(tmp_path), []),  # sys.executable: not a script
        (request(tmp_path, program=None, module="m", args=[]), []),
    ):
        assert run(req) == 0
        assert seen["config"]["user_roots"] == [norm_path(p) for p in roots]
