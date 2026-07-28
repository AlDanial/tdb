from tdb.adapters.perl.protocol import StreamParser


def test_prompt_detected():
    p = StreamParser()
    assert p.feed(b"  DB<1> ") == [("prompt", None)]


def test_marked_json_then_prompt():
    p = StreamParser()
    events = p.feed(b'TDB>>>{"a": 1}<<<TDB\n  DB<2> ')
    assert events == [("json", {"a": 1}), ("prompt", None)]


def test_marker_split_across_chunks():
    p = StreamParser()
    assert p.feed(b'TDB>>>{"file": "t') == []
    events = p.feed(b'.pl"}<<<TDB\n  DB<3> ')
    assert events == [("json", {"file": "t.pl"}), ("prompt", None)]


def test_chatter_is_text():
    p = StreamParser()
    events = p.feed(b"main::(t.pl:3):\tmy $x = 1;\n  DB<1> ")
    assert events == [("text", "main::(t.pl:3):\tmy $x = 1;\n"), ("prompt", None)]


def test_prompt_split_across_chunks():
    p = StreamParser()
    assert p.feed(b"  DB<1") == []
    assert p.feed(b"> ") == [("prompt", None)]


def test_nested_prompt_numbers():
    # perl5db uses DB<<2>> style inside nested evals
    p = StreamParser()
    assert p.feed(b"  DB<<2>> ") == [("prompt", None)]


def test_invalid_json_in_marker_is_text():
    p = StreamParser()
    events = p.feed(b"TDB>>>not json<<<TDB\n  DB<1> ")
    assert events[0][0] == "text"
    assert events[-1] == ("prompt", None)
