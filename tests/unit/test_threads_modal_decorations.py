"""Pure-logic tests for ThreadsModal's decoration/visibility helper.

Modal rendering (compose/mount, actual filtering through the DataTable)
is exercised by the integration task; this file only covers the
module-level `visible_threads` helper.
"""

from tdb.dap.types import Thread
from tdb.languages.base import ThreadDecoration
from tdb.widgets.threads_modal import visible_threads


def _decs():
    return [
        ThreadDecoration(Thread(1, "prog"), "Domain 0 (main)", False),
        ThreadDecoration(Thread(2, "prog"), None, True),
        ThreadDecoration(Thread(3, "prog"), "Domain 1", False),
    ]


def test_hidden_filtered_by_default():
    vis = visible_threads(_decs(), show_all=False)
    assert [d.thread.id for d in vis] == [1, 3]


def test_show_all_reveals_everything():
    vis = visible_threads(_decs(), show_all=True)
    assert [d.thread.id for d in vis] == [1, 2, 3]


def test_none_decorations_means_no_filtering():
    assert visible_threads(None, show_all=False) is None
