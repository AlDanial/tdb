"""Colors the Code View derives from the active Textual theme.

The Code View highlights source with Rich/Pygments rather than Textual's
own styles, so a theme switch would otherwise leave the code in monokai.
This module picks a Pygments style and the current-/cursor-line
backgrounds that suit a given theme.
"""

from __future__ import annotations

from textual.color import Color
from textual.theme import Theme

# Textual theme name -> Pygments style. Only close matches are listed;
# every other theme (including user-registered ones) falls back on its
# dark/light flag.
_PYGMENTS_FOR_THEME: dict[str, str] = {
    "textual-dark": "monokai",
    "textual-light": "default",
    "nord": "nord",
    "gruvbox": "gruvbox-dark",
    "dracula": "dracula",
    "tokyo-night": "night-owl",
    "monokai": "monokai",
    "solarized-light": "solarized-light",
    "solarized-dark": "solarized-dark",
    "atom-one-dark": "one-dark",
    "catppuccin-latte": "friendly",
}

_DARK_FALLBACK = "monokai"
_LIGHT_FALLBACK = "default"

# Line backgrounds for themes whose colors are ANSI (terminal palette)
# references, which can't be tinted: (current_line_bg, cursor_line_bg).
_ANSI_DARK = ("rgb(120,100,30)", "rgb(60,60,80)")
_ANSI_LIGHT = ("rgb(250,232,170)", "rgb(215,225,245)")


def pygments_style_for(theme: Theme) -> str:
    """Pygments style name to highlight source with under `theme`."""
    style = _PYGMENTS_FOR_THEME.get(theme.name)
    if style is not None:
        return style
    return _DARK_FALLBACK if theme.dark else _LIGHT_FALLBACK


def _blend(base: Color, color: str, factor: float) -> str:
    r, g, b = base.blend(Color.parse(color), factor).rgb
    return f"rgb({r},{g},{b})"


def line_backgrounds(theme: Theme) -> tuple[str, str]:
    """(current_line_bg, cursor_line_bg) as Rich ``rgb(r,g,b)`` strings.

    Both are tints of the theme's background, so they stay dark on dark
    themes and light on light ones: the current (stopped-at) line takes
    the warning color, the cursor line the primary color.
    """
    base = Color.parse(theme.background or ("#1e1e1e" if theme.dark else "#ffffff"))
    warning = Color.parse(theme.warning or "#ffa62b")
    primary = Color.parse(theme.primary)
    if any(c.ansi is not None for c in (base, warning, primary)):
        # ANSI colors (the ansi-* themes) don't blend: Color.blend returns
        # the raw tint, e.g. pure red. Use fixed washes instead.
        return _ANSI_DARK if theme.dark else _ANSI_LIGHT
    current = _blend(base, warning.hex, 0.35)
    cursor = _blend(base, primary.hex, 0.20)
    return current, cursor
