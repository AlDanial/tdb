from tdb.adapters.seqs import SeqTranslator


def test_client_request_roundtrip():
    t = SeqTranslator()
    fwd = t.client_request_to_upstream(
        {"seq": 41, "type": "request", "command": "next"}
    )
    assert fwd["seq"] == 1 and fwd["command"] == "next"
    resp = t.upstream_response_to_client(
        {
            "seq": 9,
            "type": "response",
            "request_seq": 1,
            "command": "next",
            "success": True,
        }
    )
    assert resp["request_seq"] == 41 and resp["seq"] == 1


def test_proxy_originated_response_is_swallowed():
    t = SeqTranslator()
    assert (
        t.upstream_response_to_client(
            {
                "seq": 1,
                "type": "response",
                "request_seq": 999,
                "command": "initialize",
                "success": True,
            }
        )
        is None
    )


def test_events_are_resequenced_monotonically():
    t = SeqTranslator()
    e1 = t.upstream_event_to_client({"seq": 50, "type": "event", "event": "output"})
    e2 = t.upstream_event_to_client({"seq": 51, "type": "event", "event": "stopped"})
    assert (e1["seq"], e2["seq"]) == (1, 2)


def test_reverse_request_roundtrip():
    t = SeqTranslator()
    fwd = t.upstream_request_to_client(
        {"seq": 7, "type": "request", "command": "runInTerminal"}
    )
    back = t.client_response_to_upstream(
        {
            "seq": 3,
            "type": "response",
            "request_seq": fwd["seq"],
            "command": "runInTerminal",
            "success": True,
        }
    )
    assert back["request_seq"] == 7


def test_client_response_without_mapping_is_swallowed():
    t = SeqTranslator()
    assert (
        t.client_response_to_upstream(
            {
                "seq": 3,
                "type": "response",
                "request_seq": 12,
                "command": "x",
                "success": True,
            }
        )
        is None
    )


def test_inputs_are_not_mutated():
    t = SeqTranslator()
    msg = {"seq": 5, "type": "request", "command": "threads"}
    t.client_request_to_upstream(msg)
    assert msg["seq"] == 5
