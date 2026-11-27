# `K` key help and documentation search keys

Branch `small-fixes`. Two small, independent TUI changes.

## 1. `K`: the keys for the current view

**Why.** Some views have more key bindings than the footer can show. The Code
View in Debug mode is the clearest case. The user should not have to scroll
the documentation to find them.

**Goal.** Pressing `K` opens a modal that lists every key working in the view
that has focus. ESC or `q` closes it.

**Non-goals.**
- No `K` modal in the Code View's Edit mode: the editor owns every key there.
- No scheme picker. Configure > Keybindings keeps it, unchanged.

**Anti-goal.** `K` must never be taken away from text the user types. In the
Evaluate console or any other text input, `K` stays a typed `K`.

**Approaches considered.**
1. **Read the keys live (picked).** The modal is built from what is in effect
   for the focused view. It stays correct as bindings change.
2. **Extend Configure > Keybindings** with a section per view. Rejected: `K`
   would open a long list and the scheme picker, not this view's keys.
3. **A hand-written list per view.** Rejected: it goes stale whenever a
   binding changes.

**Design.**
- **Code View, Navigation or Debug mode.** "This view" lists that mode's full
  table for the current scheme, from `KeybindingConfig.format_bindings(mode)`,
  the same source as Configure > Keybindings. The Code View's footer-hint
  bindings (`footer_hint_*`) are not listed separately, since they only repeat
  that table.
- **Other main-screen views** (Console, Variables, Stack, Breakpoints). "This
  view" lists the Textual bindings in effect for the focused widget and its
  ancestors below the screen, including bindings hidden from the footer.
- **"Everywhere"** lists the screen-level and app-level bindings: focus
  switching, menus, quit and so on.
- **Display.** Each entry shows its display key and description, in the style
  of the existing Keybindings modal. Bindings without a description get one
  written; none is shown as a bare action name.
- **Footer.** `K` does not take a footer slot, which is the whole point. It is
  mentioned in the docs instead.

**Constraint.** `K` has to reach the modal from the Code View in Navigation
and Debug modes, so its key dispatch (`_on_key` through `KeybindingConfig`)
must not swallow it.

**Known limit.** The Evaluate console's focus is a text input, so `K` types
there. The modal opens from the other views.

## 2. Documentation search: navigation keys and footer

This covers both hosts of `SearchableMarkdownViewer`: Help > Documentation
(`_DocumentationModal`) and `tdb --doc`.

**Goal.** After Enter on a search term, the search bar still shows, focus is
back in the document, and the results are live. In that state:
- `n` or → goes to the next match;
- `p`, ← or `N` goes to the previous match;
- `q` leaves search mode, as ESC already does there;
- the footer shows exactly those keys, for example
  `n/→ next · p/←/N previous · q exit search`.

When search closes, the footer goes back to its normal hint.

**Design.**
- **New bindings.** `p`, ← and → are new, and `q` gains its search meaning.
  They act only while the search bar shows with focus back in the document.
  Otherwise each key does what it does today:
  - ← and → scroll sideways;
  - `p` does nothing;
  - `q` closes Help > Documentation or quits `tdb --doc`.

  The pattern is the one `action_escape_search` uses now: act when searching,
  else defer.
- **`tdb --doc`.** Its Textual `Footer` shows the search keys only while
  searching, by enabling and disabling them through `check_action` and
  refreshing the bindings when the state changes.
- **Help > Documentation.** Its fixed `#doc-footer` line is swapped for the
  search hint while searching, then restored afterwards.
- **While typing.** In the search input itself, keys still type, as now.

## Docs

- `docs/using/keybindings.md` gets the new search keys and `q`, and a line
  about `K`.
- `docs/reference/cli.md`'s `--doc` row gets the new search keys.

## Left to the builder

- How the "results showing" state notifies each host's footer: a message or a
  callback.
- The exact wording of the footer hint and the modal's title.
- How `K` is bound (an app binding or the Code View's table) so that the
  constraint above holds and text inputs keep the letter.
