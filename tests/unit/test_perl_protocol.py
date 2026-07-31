import json

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


def test_marker_with_angle_brackets_survives_chunked_feed():
    # Regression: a >4KB helper payload containing '<' (e.g. Perl
    # filehandle syntax from source()) must not be shattered into text
    # when it's split across multiple 4096-byte socket reads.
    p = StreamParser()
    text_value = "before <$fh> middle <STDIN> more <<<TD, end "
    text_value += "x" * 9000
    payload = {"text": text_value}
    body = json.dumps(payload).encode("utf-8")
    blob = b"TDB>>>" + body + b"<<<TDB\n"
    assert len(blob) > 9000

    events: list[tuple] = []
    for i in range(0, len(blob), 4096):
        events.extend(p.feed(blob[i : i + 4096]))

    assert events == [("json", payload)]


def test_partial_marker_opener_split_across_feeds():
    p = StreamParser()
    events1 = p.feed(b"some text TDB>")
    assert events1 == [("text", "some text ")]
    events2 = p.feed(b'>>{"a": 1}<<<TDB\n  DB<1> ')
    assert events2 == [("json", {"a": 1}), ("prompt", None)]


def test_lone_partial_opener_mid_text_not_held_forever():
    p = StreamParser()
    events = p.feed(b"hello TDB> world\n  DB<1> ")
    assert events == [("text", "hello TDB> world\n"), ("prompt", None)]
