"""Unit tests for tdb.inspection_full.bfs_load_full.

The BFS is the heart of the Full-Contents modal: it walks a variable
subtree breadth-first, fetches each container via a paginated DAP
abstraction, and emits sentinel placeholders for branches that exceed
depth/items/total caps. These tests pin the contract on the returned
FullContentsNode tree shape — the widget that consumes it has no logic
of its own.
"""

from __future__ import annotations

import pytest

from tdb.dap.types import Variable
from tdb.inspection_full import FullContentsNode, bfs_load_full


def _var(
    name: str, ref: int = 0, value: str = "", type: str = "",
) -> Variable:
    return Variable(
        name=name,
        value=value or f"<{name}>",
        type=type,
        variables_reference=ref,
    )


def _container(name: str, ref: int, type: str = "dict") -> Variable:
    """A child variable whose type is in the BFS recursion whitelist."""
    return Variable(
        name=name, value="{...}", type=type, variables_reference=ref,
    )


# ---- a tiny scripted-fetch helper ---------------------------------------

def _make_fetch(graph: dict[int, list[Variable]]):
    """Return an async fetch closure that serves from a static graph.

    The fetch honors `start`/`count` so the "load more" paging contract
    is exercised. Records every call into a list on the returned object
    so tests can assert which refs were visited.
    """
    calls: list[tuple[int, int, int]] = []

    async def fetch(ref: int, start: int, count: int) -> list[Variable]:
        calls.append((ref, start, count))
        page = graph.get(ref, [])
        if count == 0:
            return page[start:]
        return page[start : start + count]

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


# ---- basic shape --------------------------------------------------------

@pytest.mark.asyncio
async def test_root_zero_ref_returns_bare_node():
    fetch = _make_fetch({})
    root = await bfs_load_full(fetch, root_ref=0, root_label="x")
    assert root == FullContentsNode(label="x", ref=0, children=[])
    assert fetch.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_single_level_container_materializes_children():
    graph = {
        100: [_var("a"), _var("b"), _var("c")],
    }
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=100, root_label="d")
    assert [c.label for c in root.children] == [
        "a = <a>",
        "b = <b>",
        "c = <c>",
    ]
    assert all(c.ref == 0 for c in root.children)
    assert root.more is None


@pytest.mark.asyncio
async def test_nested_containers_traversed_breadth_first():
    # 100 -> [u (dict, ref=200), v (dict, ref=300)]
    # 200 -> [x]; 300 -> [y]
    graph = {
        100: [_container("u", ref=200), _container("v", ref=300)],
        200: [_var("x")],
        300: [_var("y")],
    }
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=100, root_label="d")
    assert [c.label.split(" =")[0] for c in root.children] == [
        "u (dict)", "v (dict)",
    ]
    # u's child
    assert [g.label for g in root.children[0].children] == ["x = <x>"]
    # v's child
    assert [g.label for g in root.children[1].children] == ["y = <y>"]
    # BFS visit order should be 100, 200, 300 (parent before either grandchild)
    visited = [r for (r, _, _) in fetch.calls]  # type: ignore[attr-defined]
    assert visited == [100, 200, 300]


# ---- depth_cap ----------------------------------------------------------

@pytest.mark.asyncio
async def test_depth_cap_stops_recursion_and_emits_more_zero():
    # Build a linear chain: 1 -> 2 -> 3 -> 4 (each with one container child)
    graph = {
        1: [_container("n2", ref=2)],
        2: [_container("n3", ref=3)],
        3: [_container("n4", ref=4)],
        4: [_var("leaf")],
    }
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=1, root_label="r", depth_cap=2)
    # depth 0: root (ref=1, fetched). depth 1: n2 (ref=2, fetched).
    # depth 2: n3 (ref=3) is hit by queue but depth>=depth_cap; emits more=(3,0).
    # The fetch at depth 1 happened (depth 1 < 2), then n3 popped at depth=2.
    n2 = root.children[0]
    assert n2.label.startswith("n2")
    n3 = n2.children[0]
    assert n3.label.startswith("n3")
    assert n3.more == (3, 0)
    assert n3.children == []
    # We never fetched ref=3 or ref=4
    visited = [r for (r, _, _) in fetch.calls]  # type: ignore[attr-defined]
    assert visited == [1, 2]


# ---- items_cap ----------------------------------------------------------

@pytest.mark.asyncio
async def test_items_cap_emits_load_more_sentinel():
    # Container with 7 children, cap at 3 → page returns 3, full_page=True,
    # so parent gets more=(ref, 3).
    graph = {
        50: [_var(f"k{i}") for i in range(7)],
    }
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=50, root_label="d", items_cap=3)
    assert [c.label.split(" =")[0] for c in root.children] == ["k0", "k1", "k2"]
    assert root.more == (50, 3)


@pytest.mark.asyncio
async def test_items_cap_not_triggered_when_exactly_full():
    # 3 children, items_cap=3 → full_page=True, so we *do* emit a sentinel
    # even though the page exactly fills the cap. This is the documented
    # over-report case (heuristic). The "load more" click will return [].
    graph = {50: [_var("a"), _var("b"), _var("c")]}
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=50, root_label="d", items_cap=3)
    assert root.more == (50, 3)


@pytest.mark.asyncio
async def test_items_cap_not_triggered_when_undercount():
    graph = {50: [_var("a"), _var("b")]}
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=50, root_label="d", items_cap=10)
    assert root.more is None


# ---- total_cap ----------------------------------------------------------

@pytest.mark.asyncio
async def test_total_cap_marks_truncated_branches():
    # 3-wide × 3-deep balanced tree = ~13 nodes if not capped.
    graph = {
        1: [_container("a", ref=10), _container("b", ref=20), _container("c", ref=30)],
        10: [_container("a1", ref=11), _var("a2"), _var("a3")],
        11: [_var("aa1"), _var("aa2"), _var("aa3")],
        20: [_var("b1"), _var("b2"), _var("b3")],
        30: [_var("c1"), _var("c2"), _var("c3")],
    }
    fetch = _make_fetch(graph)
    # Budget so tight that we drain queue partially.
    root = await bfs_load_full(fetch, root_ref=1, root_label="r", total_cap=4)
    # Exactly 4 children get appended to a `children` list across the whole
    # tree (excluding the root sentinel). The remaining queued containers
    # never get fetched and so they themselves are flagged.
    appended = sum(len(n.children) for n in _walk(root))
    assert appended == 4
    # At least one truncated marker should exist (either on a parent whose
    # children fetch was budget-skipped, or on an in-loop child that broke
    # out when the budget hit zero mid-page).
    truncated = [n for n in _walk(root) if n.truncated_by_budget]
    assert truncated, "Expected at least one truncated_by_budget node"


def _walk(node: FullContentsNode):
    yield node
    for c in node.children:
        yield from _walk(c)


# ---- cache write-through -----------------------------------------------

@pytest.mark.asyncio
async def test_cache_writer_called_for_complete_page_only():
    # 100: 7 entries; 200: 2 entries.
    # items_cap=3 → ref=100 is a partial page (full_page=True) → NO cache write.
    # ref=200's page is undercount → cache write SHOULD happen.
    graph = {
        100: [
            _container(f"k{i}", ref=200) if i == 0 else _var(f"k{i}")
            for i in range(7)
        ],
        200: [_var("x"), _var("y")],
    }
    fetch = _make_fetch(graph)
    writes: list[tuple[int, list[Variable]]] = []
    await bfs_load_full(
        fetch,
        root_ref=100,
        root_label="r",
        items_cap=3,
        cache_writer=lambda ref, vars_: writes.append((ref, list(vars_))),
    )
    cached_refs = [ref for (ref, _) in writes]
    assert 200 in cached_refs
    assert 100 not in cached_refs


@pytest.mark.asyncio
async def test_cache_writer_skipped_when_none():
    graph = {1: [_var("x")]}
    fetch = _make_fetch(graph)
    # Should not raise.
    await bfs_load_full(fetch, root_ref=1, root_label="r", cache_writer=None)


# ---- non-container leaves are verbatim ---------------------------------

@pytest.mark.asyncio
async def test_leaves_added_verbatim_with_no_fetch():
    graph = {1: [_var("scalar_a", value="42"), _var("scalar_b", value="'hi'")]}
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=1, root_label="r")
    # Each leaf carries the format_label string and ref=0.
    assert root.children[0].label == "scalar_a = 42"
    assert root.children[0].ref == 0
    assert root.children[0].children == []
    assert root.children[1].label == "scalar_b = 'hi'"
    # Only the root was fetched.
    visited = [r for (r, _, _) in fetch.calls]  # type: ignore[attr-defined]
    assert visited == [1]


# ---- post-mortem path simulation ---------------------------------------

@pytest.mark.asyncio
async def test_post_mortem_synchronous_path_produces_same_tree():
    # Simulate the post-mortem call shape: a fake state.variables dict
    # wrapped in an async fetch. Tree shape should be identical to the
    # live-DAP path for the same data.
    state_vars: dict[int, list[Variable]] = {
        1: [_container("inner", ref=2)],
        2: [_var("leaf", value="42")],
    }

    async def pm_fetch(ref: int, start: int, count: int) -> list[Variable]:
        page = state_vars.get(ref, [])
        end = start + count if count else len(page)
        return page[start:end]

    root_pm = await bfs_load_full(pm_fetch, root_ref=1, root_label="r")
    # Compare with live shape.
    live_fetch = _make_fetch(state_vars)
    root_live = await bfs_load_full(live_fetch, root_ref=1, root_label="r")
    assert _structure(root_pm) == _structure(root_live)


def _structure(node: FullContentsNode) -> tuple:
    """Comparable shape: (label, ref, more, [children...])."""
    return (node.label, node.ref, node.more, tuple(_structure(c) for c in node.children))


# ---- filtering: synthetic groups, dunders, methods ---------------------

@pytest.mark.asyncio
async def test_special_and_function_variables_groups_are_dropped():
    # debugpy adds these synthetic groups under every object-instance ref.
    # They must never appear in the modal tree.
    graph = {
        1: [
            _var("real_field", value="42"),
            _var("special variables", ref=900),
            _var("function variables", ref=901),
        ],
    }
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=1, root_label="obj")
    labels = [c.label for c in root.children]
    assert "real_field = 42" in labels
    assert not any("special variables" in lbl for lbl in labels)
    assert not any("function variables" in lbl for lbl in labels)
    # Neither synthetic group's children were ever fetched.
    visited = [r for (r, _, _) in fetch.calls]  # type: ignore[attr-defined]
    assert 900 not in visited
    assert 901 not in visited


@pytest.mark.asyncio
async def test_dunder_names_are_dropped():
    graph = {
        1: [
            _var("__class__", value="<class 'X'>"),
            _var("__dict__", value="{...}", ref=2, type="dict"),
            _var("__module__", value="'__main__'"),
            _var("name", value="'visible'"),
        ],
    }
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=1, root_label="obj")
    labels = [c.label for c in root.children]
    assert labels == ["name = 'visible'"]
    # Even though __dict__ is type=dict and would be recursed, the dunder
    # name filter eliminates it before recursion.
    visited = [r for (r, _, _) in fetch.calls]  # type: ignore[attr-defined]
    assert visited == [1]


@pytest.mark.asyncio
async def test_method_like_values_are_dropped():
    graph = {
        1: [
            _var("field", value="42"),
            _var("foo", value="<bound method X.foo of <X object>>", ref=10),
            _var("bar", value="<function X.bar at 0x7f...>", ref=11),
            _var("baz", value="<built-in method X.baz>", ref=12),
        ],
    }
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=1, root_label="obj")
    labels = [c.label for c in root.children]
    assert labels == ["field = 42"]


# ---- container gating ---------------------------------------------------

@pytest.mark.asyncio
async def test_non_container_with_ref_becomes_on_demand_sentinel():
    # A dataclass-like child has ref>0 but type isn't a known container.
    # It should be shown as a leaf with `more=(ref, 0)` so the modal
    # renders an "Enter to expand" placeholder. Its children must NOT
    # be pre-fetched.
    graph = {
        1: [
            Variable(name="dc", value="Point(x=1, y=2)", type="Point",
                     variables_reference=200),
        ],
        200: [
            _var("x", value="1", type="int"),
            _var("y", value="2", type="int"),
        ],
    }
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=1, root_label="r")
    assert len(root.children) == 1
    dc = root.children[0]
    assert dc.label.startswith("dc")
    assert dc.more == (200, 0)            # on-demand sentinel
    assert dc.children == []              # not pre-fetched
    visited = [r for (r, _, _) in fetch.calls]  # type: ignore[attr-defined]
    assert visited == [1]                 # ref=200 was NOT fetched


@pytest.mark.asyncio
async def test_recursion_skips_through_non_containers():
    # A dict containing a custom class containing a dict.
    # Only the outer dict is pre-fetched; the custom class becomes a
    # sentinel; the inner dict is reachable only on user demand.
    graph = {
        1: [Variable(name="key", value="Wrap(d={...})", type="Wrap",
                     variables_reference=200)],
        200: [_container("inner", ref=300)],
        300: [_var("leaf", value="42")],
    }
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=1, root_label="r")
    wrap = root.children[0]
    assert wrap.more == (200, 0)
    assert wrap.children == []
    visited = [r for (r, _, _) in fetch.calls]  # type: ignore[attr-defined]
    assert 200 not in visited
    assert 300 not in visited


@pytest.mark.asyncio
async def test_list_of_dicts_recurses_fully():
    # list[dict] is the canonical "pre-expand everything" case.
    graph = {
        1: [_container("[0]", ref=10), _container("[1]", ref=11)],
        10: [_var("a", value="1")],
        11: [_var("b", value="2")],
    }
    fetch = _make_fetch(graph)
    root = await bfs_load_full(fetch, root_ref=1, root_label="r")
    assert [g.label for g in root.children[0].children] == ["a = 1"]
    assert [g.label for g in root.children[1].children] == ["b = 2"]


# ---- fetch failure leaves parent intact --------------------------------

@pytest.mark.asyncio
async def test_fetch_exception_leaves_parent_empty_not_crashed():
    async def boom(ref: int, start: int, count: int):
        raise RuntimeError("disconnected")

    root = await bfs_load_full(boom, root_ref=42, root_label="r")
    assert root.children == []
    assert root.label == "r"
    assert root.ref == 42
