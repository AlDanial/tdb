# Dynamic color-theme selection — design

Branch: `dynamic-color-selection`. Size: small change.

## Intent

Choosing a theme today means opening Configure → Color Theme and picking a
name from Textual's full-screen search palette, once per try. That palette
doesn't preview anything and it hides the code. The user wants to step
through themes from the Code View and see each one applied right away to
the screen they're actually looking at.

## Goals

- Open a small theme list from the Code View with one key. The code stays
  visible while the list is open.
- Highlighting a theme (Up/Down) applies it to the whole app immediately.
- Enter keeps the theme and saves it to config. Esc reverts to the theme
  that was active when the list opened.
- Code syntax colors follow the theme, instead of always using monokai.

## Non-goals

- New or custom themes. Only the themes registered with Textual are listed.
- Changes to `tdb --vi` (`vi_app.py`). It keeps its own command-palette
  theme switch.

## Anti-goals

- Saving to config while previewing. Only Enter saves.
- Leaving the app in a preview theme. Every way out of the list other than
  Enter (Esc, q, Ctrl+Q, Ctrl+C, restart, the debuggee exiting) restores
  the original theme.

## Approaches

- **A. Bare next/previous keys.** Each press switches to the next or
  previous theme and saves it, with a toast showing the name. Simple, but
  there's no way back to the starting theme.
- **B. Live-preview strip (chosen).** A docked list applies each theme as
  it's highlighted, Enter keeps it and Esc reverts. Trying a theme costs
  nothing.

The user chose B.

## Design

- **Key:** `T` in the Code View's Debug and Navigation modes. It's free in
  the vim, emacs and Notepad-style schemes (lowercase `t` is run-to-cursor).
  Configure → Color Theme opens the same strip instead of Textual's palette.
- **Strip:** a narrow list docked on the right side of the screen, so the
  Code View on the left stays visible. The current theme is pre-highlighted
  and the list is in alphabetical order. The footer shows
  `↑↓ preview  Enter keep  Esc revert`.
- **Persistence:** `watch_theme` stops saving unconditionally. While the
  strip is open, changes aren't saved. Enter saves explicitly.
- **Syntax colors:** a table maps each Textual theme name to a Pygments
  style (e.g. `solarized-light` → `solarized-light`, `nord` → `nord`,
  `dracula` → `dracula`, `gruvbox` → `gruvbox-dark`). Themes with no
  close match fall back on the theme's `dark` flag: dark → `monokai`,
  light → `default`. When the theme changes, CodeView re-runs
  `_highlight_source` and re-renders. Startup uses the restored theme.
- **Current-line / cursor-line backgrounds** (`code_view.py:1321-1334`,
  hardcoded `rgb(120,100,30)` / `rgb(60,60,80)`) are dark-only and would
  look wrong on light themes. They'll be derived from the theme's colors
  instead (my call: needed for the result to look right; the builder picks
  the exact theme variables).

## Left to the builder

- The exact Pygments mapping for each theme, and the strip's width and
  styling.
- Making the strip a ModalScreen with a transparent background vs. a
  docked widget. Either is fine if it meets the exit-path rule above.

## Testing

Pilot tests: open with `T` → Down changes `app.theme` → Esc restores the
original and nothing is saved; Enter saves. Same for the menu path. Run
the exit-path checks for each dismissal route listed in the anti-goals.
The syntax style changes with the theme, and the fallback is used for an
unmapped theme. The full suite passes under `uv run pytest`.
