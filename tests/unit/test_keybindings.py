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
    # The vim "NG" hint should show up via the goto_end action.
    displays = {d for d, _ in nav_pairs}
    assert "[N]G" in displays


def test_lowercase_g_unbound_in_vim_nav():
    """Lowercase `g` was removed (only `G` / `NG` are bound) — confirm."""
    cfg = KeybindingConfig.from_scheme("vim")
    assert cfg.lookup(Mode.NAVIGATION, "g") is None


def test_vim_ctrl_f_b_page_motions():
    """Vim's classic Ctrl-F / Ctrl-B page motions are bound in nav mode."""
    cfg = KeybindingConfig.from_scheme("vim")
    assert cfg.lookup(Mode.NAVIGATION, "ctrl+f") == "page_down"
    assert cfg.lookup(Mode.NAVIGATION, "ctrl+b") == "page_up"
    # DEBUG mode does not bind them — ctrl+b falls through to the app
    # binding (focus_breakpoints). ctrl+f is unbound app-wide so it's
    # silently dropped.
    assert cfg.lookup(Mode.DEBUG, "ctrl+f") is None
    assert cfg.lookup(Mode.DEBUG, "ctrl+b") is None


def test_emacs_ctrl_f_b_page_motions():
    """Emacs scheme: ctrl+b really maps to page_up (was previously
    misnamed `ctrl+b_emacs` and never matched real keypresses)."""
    cfg = KeybindingConfig.from_scheme("emacs")
    assert cfg.lookup(Mode.NAVIGATION, "ctrl+f") == "page_down"
    assert cfg.lookup(Mode.NAVIGATION, "ctrl+b") == "page_up"


def test_edit_mode_exists_and_lookup_is_inert():
    cfg = KeybindingConfig.from_scheme("vim")
    assert Mode.EDIT.value == "Edit"
    # Edit mode never routes keys through the nav/debug/shared tables:
    # the editor widget owns every key while it has focus.
    assert cfg.lookup(Mode.EDIT, "up") is None
    assert cfg.lookup(Mode.EDIT, "q") is None
    assert cfg.lookup(Mode.EDIT, "j") is None


def test_scheme_labels():
    from tdb.keybindings import scheme_label

    assert scheme_label("vim") == "Vim"
    assert scheme_label("emacs") == "Emacs"
    assert scheme_label("default") == "Notepad-style"
    assert scheme_label("bogus") == "bogus"


def test_format_bindings_edit_mode_per_scheme():
    notepad = KeybindingConfig.from_scheme("default").format_bindings(Mode.EDIT)
    emacs = KeybindingConfig.from_scheme("emacs").format_bindings(Mode.EDIT)
    vim = KeybindingConfig.from_scheme("vim").format_bindings(Mode.EDIT)
    keys = lambda rows: [k for k, _ in rows]
    assert "Ctrl+S" in keys(notepad) and "Ctrl+X Ctrl+S" not in keys(notepad)
    assert "Ctrl+X Ctrl+S" in keys(emacs)
    assert ":w" in keys(vim) and "i a I A o O" in keys(vim)
    for rows in (notepad, emacs, vim):
        assert all(isinstance(k, str) and isinstance(d, str) for k, d in rows)
