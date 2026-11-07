"""DAP messages are cleaned of terminal controls on arrival, but text that
tdb decodes further (JSON, a Python repr) can carry them escaped, as
`\\u001b` or `'\\x1b'`, past that cleaning. What it decodes must be cleaned
again, since it is the debuggee's to write."""

import json

from tdb.app_helpers import unquote_dap_string
from tdb.inspection import parse_process_json
from tdb.rust_concurrency.probes.base import parse_probe_output

OSC_52 = "\x1b]52;c;ZXZpbA==\x07"


def test_process_names_from_the_debuggee_are_cleaned():
    payload = json.dumps([{"name": f"worker{OSC_52}", "pid": 7}])
    (proc,) = parse_process_json(repr(payload))
    assert proc.name == "worker"


def test_docstrings_from_the_debuggee_are_cleaned():
    assert unquote_dap_string(repr(f"doc{OSC_52} text")) == "doc text"


def test_ordinary_docstrings_keep_their_layout():
    assert unquote_dap_string(repr("line one\n\tline two")) == "line one\n\tline two"


def test_rust_probe_text_is_cleaned():
    envelope = {"rust_version": "1.98.0", "threads": [], "warnings": [f"w{OSC_52}"]}
    result = parse_probe_output("TDB_RUST_JSON:" + json.dumps(envelope) + "\n")
    assert result.warnings == ("w",)
