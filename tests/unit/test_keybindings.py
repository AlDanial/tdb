"""Unit tests for tdb.keybindings."""

from __future__ import annotations

from tdb.keybindings import KeybindingConfig, Mode


def test_from_scheme_vim_default():
    cfg = KeybindingConfig.from_scheme("vim")
    assert cfg.scheme == "vim"
    # vim navigation knows j/k
    assert cfg.lookup(Mode.NAVIGATION, "j") == "cursor_down"
    assert cfg.lookup(Mode.NAVIGATION, "k") == "cursor_up"


def test_from_scheme_emacs_overrides_navigation():
    cfg = KeybindingConfig.from_scheme("emacs")
    assert cfg.scheme == "emacs"
    assert cfg.lookup(Mode.NAVIGATION, "ctrl+n") == "cursor_down"
    # emacs scheme does NOT bind plain j/k
    assert cfg.lookup(Mode.NAVIGATION, "j") is None


def test_from_scheme_default_minimal():
    cfg = KeybindingConfig.from_scheme("default")
    assert cfg.scheme == "default"
    # Plain "default" has no j/k; only the search/paragraph keys.
    assert cfg.lookup(Mode.NAVIGATION, "j") is None
    assert cfg.lookup(Mode.NAVIGATION, "slash") == "search"


def test_from_scheme_unknown_falls_back_to_vim():
    cfg = KeybindingConfig.from_scheme("not-a-real-scheme")
    assert cfg.scheme == "vim"
    assert cfg.lookup(Mode.NAVIGATION, "j") == "cursor_down"


def test_debug_mode_lookup():
    cfg = KeybindingConfig.from_scheme("vim")
    assert cfg.lookup(Mode.DEBUG, "n") == "step_over"
    assert cfg.lookup(Mode.DEBUG, "s") == "step_in"
    assert cfg.lookup(Mode.DEBUG, "c") == "continue_"
    # Both `f` and `r` alias to step_out
    assert cfg.lookup(Mode.DEBUG, "f") == "step_out"
    assert cfg.lookup(Mode.DEBUG, "r") == "step_out"


def test_shared_keys_resolve_in_either_mode():
    cfg = KeybindingConfig.from_scheme("vim")
    assert cfg.lookup(Mode.NAVIGATION, "q") == "quit"
    assert cfg.lookup(Mode.DEBUG, "q") == "quit"
    assert cfg.lookup(Mode.NAVIGATION, "pageup") == "page_up"


def test_unknown_key_returns_none():
    cfg = KeybindingConfig.from_scheme("vim")
    assert cfg.lookup(Mode.NAVIGATION, "Q") is None
    assert cfg.lookup(Mode.DEBUG, "ctrl+x") is None


def test_format_bindings_returns_pairs():
    cfg = KeybindingConfig.from_scheme("vim")
    nav_pairs = cfg.format_bindings(Mode.NAVIGATION)
    assert all(isinstance(p, tuple) and len(p) == 2 for p in nav_pairs)
    # The vim "Ng" hint should show up via the goto_line action.
    displays = {d for d, _ in nav_pairs}
    assert "[N]g" in displays
