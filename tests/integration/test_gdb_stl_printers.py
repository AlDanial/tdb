"""End-to-end: tdb's libstdc++ pretty-printer helper under a real
`gdb -i dap`.

A gdb built into its own prefix never auto-loads the printers gcc ships,
so `std::vector<Result>` shows as `_M_impl` pointer soup. tdb sources
`tdb.adapters.native.gdb_stl_printers` at gdb startup, which delegates to
gcc's printers when it can find them and otherwise to a bundled
fallback. $TDB_GDB_STL_PRINTERS selects the mode (see that module).

Session plumbing mirrors tests/integration/test_gdb_session.py.
"""

from __future__ import annotations

import asyncio
import glob
import re
import shutil
import subprocess

import pytest

from tdb.adapters.native.gdb_stl_printers import ENV_VAR, FIXED_CANDIDATE_GLOBS
from tdb.dap.types import SourceBreakpoint, Variable
from tdb.languages.cpp import build_cpp_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController


def _gdb_supports_dap() -> bool:
    gdb = shutil.which("gdb")
    if gdb is None:
        return False
    out = subprocess.run([gdb, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.\d+", out)
    return bool(m) and int(m.group(1)) >= 14


def _system_printers_present() -> bool:
    return any(glob.glob(pattern) for pattern in FIXED_CANDIDATE_GLOBS)


pytestmark = pytest.mark.skipif(
    not _gdb_supports_dap() or shutil.which("g++") is None,
    reason="gdb >= 14 or g++ not installed",
)

WAIT = 20.0

CPP_SRC = """\
#include <deque>
#include <list>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>
struct Result { std::string digits; double seconds; };
int main() {
    std::vector<int> v{1, 2, 3};
    std::string s = "hi";
    std::map<std::string, int> m;
    m["a"] = 1;
    m["b"] = 2;
    std::set<int> st{5, 6};
    std::list<int> li{7, 8};
    std::deque<int> dq{9, 10};
    std::unordered_map<int, int> um;
    um[1] = 11;
    auto p = std::make_shared<int>(7);
    std::unique_ptr<int> u(new int(9));
    std::optional<int> o = 42;
    std::vector<Result> results(2);
    results[0].digits = "314";
    int total = v.size() + s.size() + m.size() + st.size() + li.size()
        + dq.size() + um.size() + *p + *u + *o + results.size();
    return total;
}
"""
BP_LINE = 29  # return total;


@pytest.fixture(scope="module")
def cpp_binary(tmp_path_factory):
    src = tmp_path_factory.mktemp("stlsrc") / "stl.cpp"
    src.write_text(CPP_SRC)
    binary = src.parent / "stl"
    subprocess.run(["g++", "-g", "-O0", "-o", str(binary), str(src)], check=True)
    return str(binary), str(src)


@pytest.fixture
async def session():
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_cpp_profile(adapter="gdb"))
    yield ctrl, handler
    try:
        await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
    except Exception:
        pass


async def _stop_at_return(ctrl, handler, binary, src) -> dict[str, Variable]:
    """Launch, run to `return total;`, and return the locals by name."""
    ctrl.state.breakpoints.setdefault(src, []).append(SourceBreakpoint(line=BP_LINE))
    await ctrl.start(program=binary, stop_on_entry=False)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    assert await handler.wait_for_stop(timeout=WAIT)
    await ctrl.fetch_stop_info()
    assert ctrl.state.stack_frames[0].line == BP_LINE
    return {
        var.name: var
        for scope in ctrl.state.scopes
        for var in ctrl.state.variables.get(scope.variables_reference, [])
    }


async def _children(ctrl, var: Variable) -> list[Variable]:
    assert var.variables_reference > 0, var
    return await ctrl.active_client.variables(var.variables_reference)


async def _assert_pretty(ctrl, locals_: dict[str, Variable]) -> None:
    """The view a distro gdb gives: summaries plus [i] / get() children."""
    assert locals_["v"].value == "std::vector of length 3, capacity 3"
    v = await _children(ctrl, locals_["v"])
    assert [(c.name, c.value) for c in v] == [("[0]", "1"), ("[1]", "2"), ("[2]", "3")]

    assert locals_["s"].value == '"hi"'

    assert locals_["m"].value == "std::map with 2 elements"
    m = await _children(ctrl, locals_["m"])
    assert [c.value for c in m] == ['"a"', "1", '"b"', "2"]

    assert locals_["st"].value == "std::set with 2 elements"
    assert [c.value for c in await _children(ctrl, locals_["st"])] == ["5", "6"]
    assert [c.value for c in await _children(ctrl, locals_["li"])] == ["7", "8"]
    assert locals_["dq"].value == "std::deque with 2 elements"
    assert [c.value for c in await _children(ctrl, locals_["dq"])] == ["9", "10"]
    assert locals_["um"].value == "std::unordered_map with 1 element"
    assert [c.value for c in await _children(ctrl, locals_["um"])] == ["1", "11"]

    assert locals_["p"].value.startswith(
        "std::shared_ptr<int> (use count 1, weak count 0)"
    )
    assert locals_["u"].value.startswith("std::unique_ptr<int>")
    for name in ("p", "u"):
        (get,) = await _children(ctrl, locals_[name])
        assert get.name == "get()"
        assert get.value.startswith("0x")
    assert locals_["o"].value == "std::optional"
    (contained,) = await _children(ctrl, locals_["o"])
    assert (contained.name, contained.value) == ("[contained value]", "42")

    # The user's original complaint: a vector of structs holding strings.
    assert locals_["results"].value == "std::vector of length 2, capacity 2"
    first, second = await _children(ctrl, locals_["results"])
    assert (first.name, second.name) == ("[0]", "[1]")
    fields = {c.name: c.value for c in await _children(ctrl, first)}
    assert fields["digits"] == '"314"'
    assert fields["seconds"] == "0"


async def test_bundled_fallback_renders_every_container(
    session, cpp_binary, monkeypatch
):
    """Forcing the fallback shows what a user with no gcc printers gets."""
    monkeypatch.setenv(ENV_VAR, "bundled")
    binary, src = cpp_binary
    ctrl, handler = session
    locals_ = await _stop_at_return(ctrl, handler, binary, src)
    await _assert_pretty(ctrl, locals_)
    status = await ctrl.evaluate("tdb-stl-printers")
    assert "bundled fallback" in status


@pytest.mark.skipif(
    not _system_printers_present(), reason="gcc's printers not installed"
)
async def test_auto_mode_matches_the_distro_view(session, cpp_binary, monkeypatch):
    """Default mode: gcc's own printers, found by tdb or auto-loaded by
    this gdb itself (then tdb's printer stays dormant behind them)."""
    monkeypatch.setenv(ENV_VAR, "auto")
    binary, src = cpp_binary
    ctrl, handler = session
    locals_ = await _stop_at_return(ctrl, handler, binary, src)
    await _assert_pretty(ctrl, locals_)
    status = await ctrl.evaluate("tdb-stl-printers")
    # Either tdb found gcc's printers, or this gdb auto-loaded them onto
    # libstdc++.so and tdb shielded those (its own printer stays dormant).
    assert re.search(r"libstdc\+\+'s own printers|tdb-shielded", status), status
    assert "bundled" not in status


async def test_explicit_directory_without_printers_falls_back(
    session, cpp_binary, monkeypatch, tmp_path
):
    """A directory with no libstdcxx/ in it: the fallback, not an error."""
    monkeypatch.setenv(ENV_VAR, str(tmp_path))
    binary, src = cpp_binary
    ctrl, handler = session
    locals_ = await _stop_at_return(ctrl, handler, binary, src)
    await _assert_pretty(ctrl, locals_)
    status = await ctrl.evaluate("tdb-stl-printers")
    if "tdb-shielded" in status:
        # This gdb auto-loaded gcc's printers; the directory is never consulted.
        assert "not resolved yet" in status
    else:
        assert "bundled fallback" in status
        assert str(tmp_path) in status  # the searched list names it


async def test_off_registers_nothing(session, cpp_binary, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "off")
    binary, src = cpp_binary
    ctrl, handler = session
    await _stop_at_return(ctrl, handler, binary, src)
    status = await ctrl.evaluate("tdb-stl-printers")
    assert "off" in status
    names = await ctrl.evaluate(
        "python print([getattr(p, 'name', '?') for p in gdb.pretty_printers])"
    )
    assert "tdb-stl" not in names
