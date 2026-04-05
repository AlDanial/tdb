"""Keybinding configuration for tdbg.

Two modes:
  - NAVIGATION: vim-style movement with optional count prefix (e.g. 5j, 12g, G)
  - DEBUG: single-key debug commands (n, s, o, c, b, p, t)

ESC toggles between modes (when CodeView has focus).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Mode(Enum):
    NAVIGATION = "Navigation"
    DEBUG = "Debug"


@dataclass
class KeybindingConfig:
    """Maps key names to action names for each mode."""

    navigation: dict[str, str] = field(default_factory=lambda: {
        "g": "goto_line",           # Ng = jump to line N
        "G": "goto_end",            # G = jump to end of file
        "k": "cursor_up",           # k / Nk = move cursor up
        "j": "cursor_down",         # j / Nj = move cursor down
        "right_square_bracket": "paragraph_down",  # ]
        "left_square_bracket": "paragraph_up",     # [
        "slash": "search",                # /
        "question_mark": "search_back",   # ?
        "n": "search_next",              # n = next search result
        "N": "search_prev",              # N = previous search result
        "pageup": "page_up",
        "pagedown": "page_down",
    })

    debug: dict[str, str] = field(default_factory=lambda: {
        "n": "step_over",
        "s": "step_in",
        "o": "step_out",
        "c": "continue_",
        "b": "toggle_breakpoint",
        "p": "pause",
        "t": "run_to_cursor",
        "u": "stack_up",
        "d": "stack_down",
    })

    # Keys that work in both modes
    shared: dict[str, str] = field(default_factory=lambda: {
        "up": "cursor_up",
        "down": "cursor_down",
        "pageup": "page_up",
        "pagedown": "page_down",
        "home": "goto_home",
        "end": "goto_end",
    })

    def lookup(self, mode: Mode, key: str) -> str | None:
        """Return the action name for a key in the given mode, or None."""
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
        ACTION_LABELS = {
            "goto_line": "Go to line N",
            "goto_end": "Go to end of file",
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
            "stack_up": "Stack frame up (caller)",
            "stack_down": "Stack frame down (callee)",
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
        }

        bindings = self.navigation if mode == Mode.NAVIGATION else self.debug
        result = []
        for key, action in bindings.items():
            display = KEY_DISPLAY.get(key, key)
            label = ACTION_LABELS.get(action, action)
            if action in ("goto_line", "cursor_up", "cursor_down"):
                display = f"[N]{display}"
            result.append((display, label))

        # Add shared keys
        for key, action in self.shared.items():
            display = KEY_DISPLAY.get(key, key)
            label = ACTION_LABELS.get(action, action)
            result.append((display, label))

        return result
