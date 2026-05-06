"""Unit tests for build_wait_graph + find_cycles (pure transforms).

Live-asyncio holder detection is covered in test_task_collect_live.py.
These tests use synthetic AsyncTaskInfo / adjacency dicts to exercise
graph shape and cycle detection in isolation.
"""

from __future__ import annotations

from tdb.inspection import (
    AsyncTaskInfo,
    WaitTreeNode,
    build_wait_graph,
    build_wait_tree,
    find_cycles,
)


def _info(
    name: str,
    holders: list[str] | None = None,
    awaiting: str | None = None,
) -> AsyncTaskInfo:
    return AsyncTaskInfo(
        name=name, state="pending", coro="c", stack=[],
        holders=list(holders or []),
        awaiting=awaiting,
    )


# --- build_wait_graph ---------------------------------------------------

def test_build_wait_graph_empty():
    assert build_wait_graph([]) == {}


def test_build_wait_graph_single_unblocked_task():
    g = build_wait_graph([_info("A")])
    assert g == {"A": []}


def test_build_wait_graph_with_holders():
    g = build_wait_graph([
        _info("A", holders=["B"]),
        _info("B"),
    ])
    assert g == {"A": ["B"], "B": []}


def test_build_wait_graph_holders_list_is_a_copy():
    """Caller must be free to mutate returned lists without affecting
    the AsyncTaskInfo's own holders attribute."""
    info = _info("A", holders=["B"])
    g = build_wait_graph([info])
    g["A"].append("Z")
    assert info.holders == ["B"]


def test_build_wait_graph_preserves_holder_order():
    """Order matters for deterministic UI rendering — the debuggee
    already sorts holders, so build_wait_graph must not reshuffle."""
    g = build_wait_graph([_info("A", holders=["X", "Y", "Z"])])
    assert g["A"] == ["X", "Y", "Z"]


# --- find_cycles --------------------------------------------------------

def test_find_cycles_empty_graph():
    assert find_cycles({}) == []


def test_find_cycles_single_node_no_edges():
    assert find_cycles({"A": []}) == []


def test_find_cycles_linear_chain_has_no_cycle():
    assert find_cycles({"A": ["B"], "B": ["C"], "C": []}) == []


def test_find_cycles_two_node_deadlock():
    """Classic deadlock: A waits for B, B waits for A."""
    cycles = find_cycles({"A": ["B"], "B": ["A"]})
    assert cycles == [["A", "B"]]


def test_find_cycles_three_node_cycle():
    cycles = find_cycles({"A": ["B"], "B": ["C"], "C": ["A"]})
    assert cycles == [["A", "B", "C"]]


def test_find_cycles_self_loop():
    """Pathological but possible: a task somehow waiting on itself
    (e.g. re-entrant lock pattern misuse). Detected as a 1-element
    cycle so the UI can still flag it."""
    assert find_cycles({"A": ["A"]}) == [["A"]]


def test_find_cycles_singleton_without_self_edge_is_not_a_cycle():
    """SCC of size 1 without a self-edge is just a regular node."""
    assert find_cycles({"A": [], "B": []}) == []


def test_find_cycles_two_disjoint_cycles():
    cycles = find_cycles({
        "A": ["B"], "B": ["A"],
        "C": ["D"], "D": ["C"],
    })
    assert cycles == [["A", "B"], ["C", "D"]]


def test_find_cycles_cycle_with_dangling_tail():
    """A→B→A is a cycle; A→C is a tail. Cycle detection should still
    only flag {A,B}, not include C."""
    cycles = find_cycles({"A": ["B", "C"], "B": ["A"], "C": []})
    assert cycles == [["A", "B"]]


def test_find_cycles_cycle_with_dangling_head():
    """X→A, A→B→A. X is upstream of the cycle, not part of it."""
    cycles = find_cycles({"X": ["A"], "A": ["B"], "B": ["A"]})
    assert cycles == [["A", "B"]]


def test_find_cycles_handles_unknown_holder_names():
    """Holder may reference a task name not present as a key (defensive).
    Treated as terminal — no cycle, no crash."""
    cycles = find_cycles({"A": ["Ghost"]})
    assert cycles == []


def test_find_cycles_returns_sorted_for_determinism():
    """Both within-cycle (node names) and across-cycles (cycle order)
    sorting matter for stable output."""
    cycles = find_cycles({
        "Z": ["Y"], "Y": ["Z"],
        "B": ["A"], "A": ["B"],
    })
    assert cycles == [["A", "B"], ["Y", "Z"]]


def test_find_cycles_complex_scc_collapses_to_one_entry():
    """SCC of size 3 with multiple internal edges — Tarjan returns
    one component; we report it once as a sorted list of names."""
    cycles = find_cycles({
        "A": ["B", "C"],
        "B": ["C"],
        "C": ["A"],
    })
    assert cycles == [["A", "B", "C"]]


# --- end-to-end: AsyncTaskInfo → graph → cycles -------------------------

def test_pipeline_lock_holder_no_deadlock():
    tasks = [
        _info("Holder"),
        _info("Waiter", holders=["Holder"]),
    ]
    graph = build_wait_graph(tasks)
    assert find_cycles(graph) == []


def test_pipeline_two_task_deadlock():
    """A holds X, waiting on Y; B holds Y, waiting on X.
    Surface: A.holders=[B], B.holders=[A]."""
    tasks = [
        _info("A", holders=["B"]),
        _info("B", holders=["A"]),
    ]
    graph = build_wait_graph(tasks)
    cycles = find_cycles(graph)
    assert cycles == [["A", "B"]]


# --- build_wait_tree ----------------------------------------------------

def _kinds(nodes: list[WaitTreeNode]) -> list[str]:
    return [n.kind for n in nodes]


def test_build_wait_tree_empty():
    assert build_wait_tree([]) == []


def test_build_wait_tree_only_running_tasks():
    """No blocked tasks, no cycles → only the Running section."""
    sections = build_wait_tree([_info("A"), _info("B")])
    assert _kinds(sections) == ["section_running"]
    running = sections[0]
    assert running.label == "Running / unblocked (2)"
    assert _kinds(running.children) == ["task_unblocked", "task_unblocked"]
    assert [c.data for c in running.children] == ["A", "B"]


def test_build_wait_tree_blocked_with_holder():
    """Waiter blocked on Lock held by Holder. Tree:
       Blocked → Waiter → primitive → Holder
       Running → Holder
    """
    tasks = [
        _info("Holder"),
        _info("Waiter", awaiting="Lock.acquire", holders=["Holder"]),
    ]
    sections = build_wait_tree(tasks)
    assert _kinds(sections) == ["section_blocked", "section_running"]
    blocked = sections[0]
    assert blocked.label == "Blocked tasks (1)"
    [waiter] = blocked.children
    assert waiter.kind == "task"
    assert waiter.data == "Waiter"
    [primitive] = waiter.children
    assert primitive.kind == "primitive"
    assert "Lock.acquire" in primitive.label
    [holder] = primitive.children
    assert holder.kind == "task"
    assert holder.data == "Holder"
    # Holder has no awaiting → no primitive child.
    assert holder.children == []


def test_build_wait_tree_blocked_with_no_holder_identified():
    """Blocked task with empty holders renders an explicit (no holder) leaf."""
    tasks = [_info("T", awaiting="Event.wait", holders=[])]
    sections = build_wait_tree(tasks)
    [blocked] = sections
    [task_node] = blocked.children
    [primitive] = task_node.children
    [no_holder] = primitive.children
    assert no_holder.kind == "no_holder"


def test_build_wait_tree_orphan_holder():
    """Holder name references a task not in the snapshot — render as
    `orphan` leaf so the user knows the link is dangling."""
    tasks = [
        _info("T", awaiting="Lock.acquire", holders=["Ghost"]),
    ]
    sections = build_wait_tree(tasks)
    [blocked] = sections
    [task_node] = blocked.children
    [primitive] = task_node.children
    [orphan] = primitive.children
    assert orphan.kind == "orphan"
    assert orphan.data == "Ghost"
    assert "not in snapshot" in orphan.label


def test_build_wait_tree_two_task_deadlock_renders_cycle_section_and_ref():
    """A↔B deadlock: a `Deadlock cycles` section appears at the top, and
    descending into A's subtree, B's subtree, when we'd re-enter A,
    yields a `cycle_ref` leaf instead of recursing forever."""
    tasks = [
        _info("A", awaiting="Lock.acquire", holders=["B"]),
        _info("B", awaiting="Lock.acquire", holders=["A"]),
    ]
    sections = build_wait_tree(tasks)
    assert _kinds(sections) == ["section_cycles", "section_blocked"]

    # Cycles section
    cycles_sec = sections[0]
    assert cycles_sec.label == "Deadlock cycles (1)"
    [cycle_leaf] = cycles_sec.children
    assert cycle_leaf.kind == "cycle"
    assert cycle_leaf.label == "A <-> B"
    assert cycle_leaf.data == "A"  # selecting jumps to A in the table

    # Blocked section: descending A → primitive → B → primitive → A (cycle_ref)
    blocked = sections[1]
    a = next(c for c in blocked.children if c.data == "A")
    a_prim = a.children[0]
    b = a_prim.children[0]
    assert b.kind == "task"
    assert b.data == "B"
    b_prim = b.children[0]
    a_ref = b_prim.children[0]
    assert a_ref.kind == "cycle_ref"
    assert a_ref.data == "A"
    assert a_ref.children == []


def test_build_wait_tree_self_cycle_in_section():
    """Self-cycle (A waits on a primitive A holds) — Deadlocks section
    labels it explicitly so it doesn't read like a 1-task list."""
    tasks = [_info("Solo", awaiting="Lock.acquire", holders=["Solo"])]
    sections = build_wait_tree(tasks)
    cycles_sec = sections[0]
    [cycle_leaf] = cycles_sec.children
    assert cycle_leaf.label == "Solo (self-cycle)"
    assert cycle_leaf.data == "Solo"


def test_build_wait_tree_omits_empty_sections():
    """All blocked, none unblocked: only Blocked section shows."""
    tasks = [
        _info("A", awaiting="Lock.acquire", holders=["B"]),
        _info("B", awaiting="Lock.acquire", holders=[]),
    ]
    # No cycle (B doesn't wait back on A; B just has no holder).
    sections = build_wait_tree(tasks)
    assert _kinds(sections) == ["section_blocked"]


def test_build_wait_tree_node_data_is_set_for_task_kinds_only():
    """Selection sync needs `data` populated only on rows that map to
    a real task. Section/primitive/no_holder rows must have data=None."""
    tasks = [
        _info("Holder"),
        _info("Waiter", awaiting="Event.wait", holders=[]),
    ]
    sections = build_wait_tree(tasks)
    blocked = sections[0]
    assert blocked.data is None
    [waiter] = blocked.children
    assert waiter.data == "Waiter"
    [primitive] = waiter.children
    assert primitive.data is None
    [no_holder] = primitive.children
    assert no_holder.data is None
