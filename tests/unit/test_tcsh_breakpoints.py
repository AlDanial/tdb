from pathlib import Path

import pytest

from tdb.adapters.tcsh.breakpoints import bind_breakpoints
from tdb.adapters.tcsh.instrumenter import Probe, SourceMap
from tdb.adapters.tcsh.models import SourceSpan


@pytest.fixture
def source_map() -> SourceMap:
    path = Path("/work/main.csh")
    return SourceMap(
        (
            Probe(10, SourceSpan(path, 3, 3), Path("/generated/10.csh"), 0),
            Probe(11, SourceSpan(path, 5, 6), Path("/generated/11.csh"), 0),
        )
    )


def test_source_map_looks_up_canonical_paths(source_map: SourceMap) -> None:
    assert source_map.probe(11).span.start_line == 5
    assert [probe.id for probe in source_map.for_path(Path("/work/./main.csh"))] == [
        10,
        11,
    ]


def test_breakpoint_moves_to_next_safe_probe(source_map: SourceMap) -> None:
    bound = bind_breakpoints(source_map, Path("/work/main.csh"), [2, 99])

    assert (bound[0].verified, bound[0].line, bound[0].probe_id) == (True, 3, 10)
    assert (bound[1].verified, bound[1].line, bound[1].probe_id) == (False, None, None)
    assert bound[1].message == "No safe executable statement at or after this line"


def test_breakpoint_binding_preserves_duplicate_requested_lines(
    source_map: SourceMap,
) -> None:
    bound = bind_breakpoints(source_map, Path("/work/./main.csh"), [5, 5, 6])

    assert [(item.requested_line, item.line, item.probe_id) for item in bound] == [
        (5, 5, 11),
        (5, 5, 11),
        (6, None, None),
    ]
