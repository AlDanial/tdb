"""Recursive BFS materialization of a variable subtree.

Used by the Full-Contents modal (`widgets/full_contents_modal.py`) to
pre-fetch every nested container under a chosen variable so the user
sees the structure expanded in one go, rather than chevron-clicking
through dozens of `{...}` placeholders.

Pure async — does not import Textual — so the traversal is unit-testable
against an `AsyncMock` fetch. The widget renders the returned
`FullContentsNode` tree; further on-demand fetches (the "load more"
placeholder) live in the widget.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from tdb.dap.types import Variable

# Fetch signature: (ref, start, count) -> list[Variable].
# `count == 0` means "no client-side limit" — DAP convention.
FetchFn = Callable[[int, int, int], Awaitable[list[Variable]]]
CacheWriter = Callable[[int, list[Variable]], None]


# Types we descend into automatically. Anything else (custom classes,
# dataclass instances, numpy arrays, etc.) is shown as a leaf with an
# "(Enter to expand)" sentinel so the user can drill in on demand —
# avoids pre-fetching every method, dunder, and arbitrary attribute of
# every object in a dict-of-objects.
_CONTAINER_TYPES: frozenset[str] = frozenset({
    "dict", "list", "tuple", "set", "frozenset",
    "OrderedDict", "defaultdict", "deque", "Counter", "ChainMap",
    "MappingProxyType", "mappingproxy",
})

# debugpy groups dunder attrs and methods under these synthetic names
# in object-instance variable listings. Skip them everywhere — they
# never appear as real container keys/indices.
_SKIP_CHILD_NAMES: frozenset[str] = frozenset({
    "special variables",
    "function variables",
    "class variables",
    "len()",
})

_DUNDER_RE = re.compile(r"^__\w+__$")

# Values rendered by debugpy for callables. Matching here lets us drop
# method/function children even when debugpy didn't tag them via
# presentationHint.kind.
_CALLABLE_VALUE_RE = re.compile(
    r"^<(bound method|method|function|built-in (method|function)|"
    r"wrapper|method-wrapper|class method|static method|cython_function_or_method|"
    r"slot wrapper)\b"
)


def _is_displayable(var: Variable) -> bool:
    """Decide whether a child variable belongs in the Full-Contents tree.

    Drops debugpy's synthetic groups, dunder names, and values that look
    like methods/functions. Container elements (dict keys / list indices)
    survive — names like `[0]`, `'foo'`, etc. never trigger these filters.
    """
    if var.name in _SKIP_CHILD_NAMES:
        return False
    if _DUNDER_RE.match(var.name):
        return False
    if _CALLABLE_VALUE_RE.match(var.value):
        return False
    return True


def _filter_children(page: list[Variable]) -> list[Variable]:
    return [v for v in page if _is_displayable(v)]


def _is_container(var: Variable) -> bool:
    """True if we should pre-fetch this variable's children during BFS.

    Non-containers (objects, dataclasses, numpy arrays, …) still get a
    "load more" sentinel placed under them by the BFS so the user can
    opt into a one-level expansion via Enter inside the modal.
    """
    return var.variables_reference > 0 and var.type in _CONTAINER_TYPES


@dataclass
class FullContentsNode:
    """A node in the materialized subtree.

    `ref == 0` means a scalar leaf. `more` carries a (ref, next_start)
    sentinel when there are unfetched siblings or unexpanded depth — the
    modal renders these as `"… N more (Enter to load)"` placeholders and
    calls back into DAP on activation.
    """

    label: str
    ref: int
    children: list["FullContentsNode"] = field(default_factory=list)
    more: tuple[int, int] | None = None
    truncated_by_budget: bool = False


def _format_label(var: Variable) -> str:
    """Same label shape as VariableView._format_variable, kept in sync."""
    type_str = f" ({var.type})" if var.type else ""
    value = var.value
    if len(value) > 80:
        value = value[:77] + "..."
    return f"{var.name}{type_str} = {value}"


async def bfs_load_full(
    fetch: FetchFn,
    root_ref: int,
    root_label: str,
    *,
    depth_cap: int = 10,
    items_cap: int = 500,
    total_cap: int = 10_000,
    cache_writer: CacheWriter | None = None,
) -> FullContentsNode:
    """Materialize a variable subtree breadth-first.

    Parameters mirror the modal's user-facing guardrails:
      depth_cap   - stop recursing past this level; deeper containers
                    become leaves with `more=(ref, 0)`.
      items_cap   - max children fetched per container in this pass;
                    overflow becomes a `more=(ref, items_cap)` sentinel.
      total_cap   - hard ceiling on total materialized nodes; on
                    exhaustion remaining queue entries become
                    `truncated_by_budget=True` leaves.

    `cache_writer`, when supplied, is invoked once per ref with the
    fetched list — but ONLY for complete page-1 fetches (i.e., the full
    set of children fit within `items_cap`). This keeps the contract on
    `state.variables[ref]` simple: it's either absent or the canonical
    first-page slice.
    """
    root = FullContentsNode(label=root_label, ref=root_ref)
    if root_ref <= 0:
        return root

    # Each queue entry: (parent_node, ref, depth). `parent_node` is the
    # FullContentsNode whose `children` list will receive the fetched vars.
    queue: deque[tuple[FullContentsNode, int, int]] = deque()
    queue.append((root, root_ref, 0))

    nodes_remaining = total_cap

    while queue:
        parent_node, ref, depth = queue.popleft()

        if nodes_remaining <= 0:
            parent_node.truncated_by_budget = True
            continue

        if depth >= depth_cap:
            # Don't recurse further; let the modal lazy-expand on demand.
            parent_node.more = (ref, 0)
            continue

        try:
            raw_page = await fetch(ref, 0, items_cap)
        except Exception:
            # A failed fetch leaves the parent as-is. The modal renders
            # whatever was already populated; no half-state crashes.
            continue

        # Drop synthetic groups, dunders, and method-like values before
        # we ever look at the count. This is what keeps the modal fast:
        # debugpy lists every method/dunder of every object instance, so
        # without this filter a 100-element list of objects balloons into
        # thousands of nodes — most of which are noise.
        page = _filter_children(raw_page)

        # Determine if more siblings exist beyond what we fetched. We
        # compare on the *raw* page so the pagination heuristic isn't
        # thrown off by aggressive filtering of a long methods list.
        full_page = len(raw_page) == items_cap

        # Write-through to the shared cache only on complete (≤ items_cap)
        # fetches. Partial paged loads are NOT cached — see docstring.
        if cache_writer is not None and not full_page:
            cache_writer(ref, raw_page)

        for var in page:
            if nodes_remaining <= 0:
                # Stop draining this page; the rest are silently dropped.
                # A budget banner gets surfaced on the parent.
                parent_node.truncated_by_budget = True
                break
            nodes_remaining -= 1
            child = FullContentsNode(label=_format_label(var), ref=var.variables_reference)
            parent_node.children.append(child)
            if _is_container(var):
                # Pre-fetch container contents recursively.
                queue.append((child, var.variables_reference, depth + 1))
            elif var.variables_reference > 0:
                # Non-container with children (object, dataclass, …).
                # Don't pre-fetch — let the user opt in via Enter. The
                # widget renders this as "… (Enter to expand)".
                child.more = (var.variables_reference, 0)

        if full_page and nodes_remaining > 0:
            # Emit the "load more" sentinel — modal renders this as a
            # clickable placeholder that paginates via fetch(ref, items_cap, …).
            parent_node.more = (ref, items_cap)

    return root
