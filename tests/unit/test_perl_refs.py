from tdb.adapters.perl.refs import RefRegistry


def test_scope_and_object_refs_round_trip():
    reg = RefRegistry()
    r1 = reg.add_scope(0, "lexicals")
    r2 = reg.add_object(17)
    assert r1 != r2 and r1 > 0 and r2 > 0
    assert reg.get(r1) == {"kind": "scope", "frame": 0, "scope": "lexicals"}
    assert reg.get(r2) == {"kind": "object", "helper_id": 17}


def test_unknown_ref_returns_none():
    assert RefRegistry().get(99) is None


def test_reset_clears_and_restarts_ids():
    reg = RefRegistry()
    reg.add_scope(0, "globals")
    reg.reset()
    assert reg.get(1) is None
    assert reg.add_scope(1, "specials") == 1
