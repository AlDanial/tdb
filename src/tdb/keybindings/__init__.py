"""Keybinding configuration for tdb.

Three modes:
  - NAVIGATION: vim-style movement with optional count prefix (e.g. 5j, 12G)
  - DEBUG: single-key debug commands (n, s, o, c, b, p, t)
  - EDIT: the file is open in an editor widget; keys are owned by the
    editor (see tdb.widgets.code_editor), not by these tables.

ESC cycles Debug -> Navigation -> Edit -> Debug (when CodeView has focus).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Mode(Enum):
    NAVIGATION = "Navigation"
    DEBUG = "Debug"
    EDIT = "Edit"


_VIM_NAV = {
    "G": "goto_end",  # G = jump to end of file, NG = jump to line N
    "k": "cursor_up",  # k / Nk = move cursor up
    "j": "cursor_down",  # j / Nj = move cursor down
    "ctrl+f": "page_down",  # Ctrl+F = page forward (vim convention)
    "ctrl+b": "page_up",  # Ctrl+B = page back    (vim convention)
    "right_square_bracket": "paragraph_down",  # ]
    "left_square_bracket": "paragraph_up",  # [
    "slash": "search",  # /
    "question_mark": "search_back",  # ?
    "n": "search_next",  # n = next search result
    "N": "search_prev",  # N = previous search result
    "pageup": "page_up",
    "pagedown": "page_down",
}

_EMACS_NAV = {
    "ctrl+n": "cursor_down",
    "ctrl+p": "cursor_up",
    "ctrl+f": "page_down",
    "ctrl+b": "page_up",
    "ctrl+a": "goto_home",
    "ctrl+end": "goto_end",
    "ctrl+s_search": "search",
    "ctrl+r": "search_back",
    "n": "search_next",
    "N": "search_prev",
    "right_square_bracket": "paragraph_down",
    "left_square_bracket": "paragraph_up",
    "pageup": "page_up",
    "pagedown": "page_down",
}

_DEFAULT_NAV = {
    "slash": "search",
    "question_mark": "search_back",
    "n": "search_next",
    "N": "search_prev",
    "right_square_bracket": "paragraph_down",
    "left_square_bracket": "paragraph_up",
    "pageup": "page_up",
    "pagedown": "page_down",
}

_DEBUG = {
    "n": "step_over",
    "s": "step_in",
    "o": "step_out",
    "f": "step_out",
    "r": "step_out",
    "c": "continue_",
    "b": "toggle_breakpoint",
    "e": "show_traceback",
    "p": "pause",
    "t": "run_to_cursor",
    "u": "stack_up",
    "d": "stack_down",
    "L": "goto_line_prompt",
    "R": "restart",
    # vim-style cursor movement also usable in debug mode (k/j/G with
    # count prefix). Convenient for stepping the cursor without leaving
    # debug mode just to scroll a few lines.
    "k": "cursor_up",
    "j": "cursor_down",
    "G": "goto_end",
}

_SHARED = {
    "up": "cursor_up",
    "down": "cursor_down",
    "pageup": "page_up",
    "pagedown": "page_down",
    "home": "goto_home",
    "end": "goto_end",
    "q": "quit",
}

# User-facing names for the three schemes. The config value stays the
# key ("default" is what breakpoints.json / config.json persist); only
# the label says what "default" means.
SCHEME_LABELS = {"vim": "Vim", "emacs": "Emacs", "default": "Notepad-style"}


def scheme_label(scheme: str) -> str:
    return SCHEME_LABELS.get(scheme, scheme)


# Edit-mode reference tables, (key display, description). These are
# documentation for the Keybindings dialog; the live bindings are
# implemented in tdb.widgets.code_editor and must be kept in step.
_NOTEPAD_EDIT_HELP: list[tuple[str, str]] = [
    ("Arrows", "Move cursor"),
    ("Home / End", "Start / end of line"),
    ("PgUp / PgDn", "Page up / down"),
    ("Shift+Arrows", "Select text"),
    ("Delete / Backspace", "Delete right / left"),
    ("Ctrl+Z / Ctrl+Y", "Undo / redo"),
    ("Ctrl+X / Ctrl+C / Ctrl+V", "Cut / copy / paste"),
    ("Ctrl+S", "Save file"),
    ("Esc", "Leave Edit mode"),
]

_EMACS_EDIT_HELP: list[tuple[str, str]] = [
    ("Ctrl+N / Ctrl+P", "Down / up"),
    ("Ctrl+F / Ctrl+B", "Right / left"),
    ("Alt+F / Alt+B", "Word right / left"),
    ("Ctrl+A / Ctrl+E", "Start / end of line"),
    ("Alt+< / Alt+>", "Start / end of file"),
    ("Ctrl+D", "Delete right"),
    ("Ctrl+K", "Kill to end of line"),
    ("Ctrl+Y", "Yank last kill"),
    ("Ctrl+_", "Undo"),
    ("Ctrl+S / Ctrl+R", "Search forward / backward"),
    ("Ctrl+X Ctrl+S", "Save file"),
    ("Ctrl+X Ctrl+C", "Leave Edit mode"),
    ("Ctrl+G", "Cancel pending Ctrl+X"),
    ("Esc", "Leave Edit mode"),
]

_VIM_EDIT_HELP: list[tuple[str, str]] = [
    ("h j k l", "Move (count prefix allowed)"),
    ("w b e", "Word motions"),
    ("0 ^ $", "Line start / first non-blank / line end"),
    ("gg / G / NG", "File start / end / line N"),
    ("Ctrl+F / Ctrl+B", "Page down / up"),
    ("x X", "Delete char under / before cursor"),
    ("dd dw D", "Delete line / word / to end of line"),
    ("yy", "Yank line"),
    ("p P", "Put after / before"),
    ("u / Ctrl+R", "Undo / redo"),
    ("J", "Join lines"),
    ("i a I A o O", "Enter insert mode"),
    ("/ ? n N", "Search forward / backward, next / previous"),
    (":w", "Save file"),
    (":q :wq :q!", "Leave Edit mode (save / discard)"),
    (":N", "Go to line N"),
    ("Esc", "Insert -> normal; normal -> leave Edit mode"),
]


def edit_help(scheme: str) -> list[tuple[str, str]]:
    if scheme == "emacs":
        return list(_EMACS_EDIT_HELP)
    if scheme == "default":
        return list(_NOTEPAD_EDIT_HELP)
    return list(_VIM_EDIT_HELP)


@dataclass
class KeybindingConfig:
    """Maps key names to action names for each mode."""

    scheme: str = "vim"
    navigation: dict[str, str] = field(default_factory=lambda: dict(_VIM_NAV))
    debug: dict[str, str] = field(default_factory=lambda: dict(_DEBUG))

    # Keys that work in both modes
    shared: dict[str, str] = field(default_factory=lambda: dict(_SHARED))

    @classmethod
    def from_scheme(cls, scheme: str) -> KeybindingConfig:
        """Create a keybinding config from a named scheme."""
        if scheme == "emacs":
            return cls(scheme=scheme, navigation=dict(_EMACS_NAV))
        elif scheme == "default":
            return cls(scheme=scheme, navigation=dict(_DEFAULT_NAV))
        else:
            # "vim" or anything else
            return cls(scheme="vim", navigation=dict(_VIM_NAV))

    def lookup(self, mode: Mode, key: str) -> str | None:
        """Return the action name for a key in the given mode, or None.

        EDIT always returns None: while the editor widget has focus it
        owns every key, and the shared table (arrows, q, ...) must not
        intercept them.
        """
        if mode == Mode.EDIT:
            return None
        if mode == Mode.NAVIGATION:
            action = self.navigation.get(key)
            if action:
                return action
        elif mode == Mode.DEBUG:
            action = self.debug.get(key)
            if action:
                return action
        return self.shared.get(key)

    def format_bindings(self, mode: Mode) -> list[tuple[str, str]]:
        """Return (key_display, description) pairs for display."""
        if mode == Mode.EDIT:
            return edit_help(self.scheme)
        ACTION_LABELS = {
            "goto_line_prompt": "Go to line (prompt)",
            "goto_end": "Go to end of file (NG = line N)",
            "goto_home": "Go to start of file",
            "cursor_up": "Move cursor up",
            "cursor_down": "Move cursor down",
            "paragraph_down": "Next paragraph",
            "paragraph_up": "Previous paragraph",
            "search": "Search forward",
            "search_back": "Search backward",
            "search_next": "Next search result",
            "search_prev": "Previous search result",
            "page_up": "Page up",
            "page_down": "Page down",
            "step_over": "Step over",
            "step_in": "Step into",
            "step_out": "Step out",
            "continue_": "Continue",
            "toggle_breakpoint": "Toggle breakpoint",
            "pause": "Pause",
            "run_to_cursor": "Run to cursor",
            "show_traceback": "Show last exception",
            "stack_up": "Move up the stack",
            "stack_down": "Move down the stack",
            "restart": "Restart program",
            "quit": "Quit tdb",
        }
        KEY_DISPLAY = {
            "right_square_bracket": "]",
            "left_square_bracket": "[",
            "slash": "/",
            "question_mark": "?",
            "pageup": "PgUp",
            "pagedown": "PgDn",
            "up": "Up",
            "down": "Down",
            "home": "Home",
            "end": "End",
            "ctrl+n": "Ctrl+N",
            "ctrl+p": "Ctrl+P",
            "ctrl+f": "Ctrl+F",
            "ctrl+b": "Ctrl+B",
            "ctrl+a": "Ctrl+A",
            "ctrl+end": "Ctrl+End",
            "ctrl+s_search": "Ctrl+S",
            "ctrl+r": "Ctrl+R",
        }

        bindings = self.navigation if mode == Mode.NAVIGATION else self.debug
        result = []
        for key, action in bindings.items():
            display = KEY_DISPLAY.get(key, key)
            label = ACTION_LABELS.get(action, action)
            if (
                action in ("goto_end", "cursor_up", "cursor_down")
                and self.scheme == "vim"
            ):
                display = f"[N]{display}"
            result.append((display, label))

        # Add shared keys
        for key, action in self.shared.items():
            display = KEY_DISPLAY.get(key, key)
            label = ACTION_LABELS.get(action, action)
            result.append((display, label))

        return result
