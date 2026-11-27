# `K` Key Help and Doc Search Keys Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `K` opens a modal of every key for the focused view. In the docs viewer, once results show, `n`/→ go forward, `p`/←/`N` go back, `q` leaves search, and the footer says so.

**Architecture:**
- **Key help.** A collector reads `screen.active_bindings` and the focus chain. A view can supply its own list through `key_help_entries()`; the Code View does, from `KeybindingConfig`. A new modal renders the result.
- **Doc search.** The results-mode keys live on the focused document widget (`_SearchableMarkdown`). `check_action` enables them only while results show, so outside that state each key falls through to today's meaning. The viewer posts a message when results mode flips, and the hosts update their footers.

**Tech Stack:** Python 3.11+, Textual 8.2.8, pytest + pytest-asyncio (`run_test`/pilot).

**Spec:** `docs/superpowers/specs/2026-11-27-key-help-and-doc-search-keys-design.md`

## Global Constraints

- **Branch and worktree.** Branch `small-fixes` in worktree `/home/al/projects/tdbg/work-small-fixes`. Run everything from there with `uv run --extra dev ...`; that env needs the dev extra.
- **Commits.** Every commit message ends with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01MK66g6QvXUYiNpnTGLn8D4`. Never push.
- **Code View in Edit mode:** no `K` modal.
- **Text inputs** (the Evaluate console, the doc search box) keep `K`, `q`, `n`, `p` and the arrows as typed keys.
- **The existing Configure > Keybindings modal stays unchanged**, apart from sharing its line formatter.
- **Footer hint copy:** `n/→ next · p/←/N previous · q exit search`.
- **Formatting.** A PostToolUse hook runs `ruff format` on edited files. Commit its formatting.

## Review Focus

1. **`K` while another modal is open** (Threads, Documentation, the key help itself). It must not crash. In the key help modal, `K` closes it. Test in Task 1.
2. **`K` with nothing focused.** The modal shows only "Everywhere" and does not crash. Test in Task 1.
3. **The emacs scheme.** `K` in the Code View lists the emacs table, not vim's. Test in Task 1.
4. **Enter on a query with no matches.** Results mode is still on, and `q` leaves search. Test in Task 2.
5. **← and → with no search.** They still resolve to the viewer's own scroll bindings. Test in Task 2.

---

### Task 1: `K` key help modal

**Files:**
- Create: `src/tdb/widgets/key_help.py`
- Modify: `src/tdb/widgets/modals.py` (lift `_KeybindingsModal._render_bindings`'s inner `fmt` to module level)
- Modify: `src/tdb/widgets/code_view.py` (add `key_help_entries`)
- Modify: `src/tdb/app.py` (binding, action, `check_action`)
- Modify: any tdb view or app `Binding(...)` without a description; the test in Step 1 finds them
- Modify: `docs/using/keybindings.md` (near line 135, a line about `K`)
- Test: `tests/unit/test_key_help.py`

**Interfaces:**
- Produces:
  - `format_key_line(key_display: str, description: str) -> str` in `modals.py`. It is today's `fmt`, unchanged; `_KeybindingsModal` calls it.
  - `@dataclass(frozen=True) class KeyHelp` with fields `title: str`, `view: list[tuple[str, str]]` and `everywhere: list[tuple[str, str]]`.
  - `collect_key_help(screen: Screen) -> KeyHelp`.
  - `class KeyHelpModal(ModalScreen[None])`, constructed as `__init__(self, help: KeyHelp)`, with `render_text() -> str`.
  - `CodeView.key_help_entries(self) -> list[tuple[str, str]]`.
  - `TdbApp.action_key_help()` and `TdbApp.check_action(action, parameters)`.
- How `collect_key_help` decides:
  - The chain is `screen.focused` plus its ancestors, stopping before the screen.
  - The first chain node with a `key_help_entries` method supplies "This view" for itself. Its own Textual bindings are skipped.
  - For every other `ActiveBinding`:
    - a node that is the screen or the app goes to `everywhere`;
    - a node in the chain goes to `view`.
  - Entries are `(app.get_key_display(binding), binding.description)`, in `active_bindings` order. An empty description is omitted.
  - `title` is `f"Keys: {plain}"`, where `plain` is the markup-stripped `border_title` of the nearest chain node that has one. With no such node, it is `"Keys"`.
- `CodeView.key_help_entries` returns `[("Esc", "Next mode: Debug → Navigate → Edit")] + self.keybindings.format_bindings(self.mode)`.
- `KeyHelpModal`:
  - `BINDINGS` are `escape`, `q` and `K`, all bound to dismiss.
  - Its body has the title, a "This view" section, then an "Everywhere" section, using `format_key_line`. It is styled like `_KeybindingsModal`, in a `VerticalScroll`.
- `TdbApp`:
  - Add `Binding("K", "key_help", "Keys", show=False)` to `BINDINGS`.
  - `action_key_help` pushes `KeyHelpModal(collect_key_help(self.screen))`.
  - `check_action` returns `False` for `"key_help"` when focus is within `#code-view` and its `mode is Mode.EDIT`. Everything else goes to `super().check_action`.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_key_help.py`
  - **Fixture.** Reuse `tdb_app` from `tests/unit/test_searchable_markdown.py`. It monkeypatches `DebugController.start`/`stop` and builds `TdbApp(program="", config=TdbConfig())`. Copy it in, or move it to a shared conftest if one already holds similar fixtures. Use `run_test(size=(120, 40))`.
  - **Tests:**
    - `test_k_in_code_view_debug_lists_full_debug_table`:
      - Focus the code view (`app.action_focus_code()`).
      - `collect_key_help(app.screen).view == code_view.key_help_entries()`, and that list equals `[("Esc", "Next mode: Debug → Navigate → Edit")] + code_view.keybindings.format_bindings(Mode.DEBUG)`.
      - The title starts with `"Keys: Code"`.
      - Pressing `K` makes `app.screen` a `KeyHelpModal` whose `render_text()` contains `"Step over"` and no `"footer_hint"`.
    - `test_k_in_code_view_navigation_lists_navigation_table`: set `code_view.mode = Mode.NAVIGATION`. `view[1:] == code_view.keybindings.format_bindings(Mode.NAVIGATION)`.
    - `test_k_lists_emacs_table_for_emacs_scheme` (Review Focus 3): set `code_view.keybindings = KeybindingConfig.from_scheme("emacs")` in Navigation mode. `view[1:] == KeybindingConfig.from_scheme("emacs").format_bindings(Mode.NAVIGATION)`.
    - `test_no_modal_in_code_view_edit_mode`: set `code_view.mode = Mode.EDIT` with focus in the code view, press `K`. `app.screen` is not a `KeyHelpModal`.
    - `test_k_in_breakpoints_lists_its_keys`: `app.action_focus_breakpoints()`, then `K`. `"Enable/Disable"`, `"Clear All"` and `"Disable All"` are under "This view", and `"Quit"` is under "Everywhere". Check with `collect_key_help(app.screen)` before pressing, or inspect the modal's `KeyHelp`.
    - `test_k_typed_in_evaluate_console`: `app.action_focus_eval()`, press `K`. The focused `Input` has `value == "K"` and no modal opened.
    - `test_every_tdb_binding_has_a_description`: run `app.action_focus_<view>()` for code, console, variables, stack and breakpoints. Each `ActiveBinding` in `app.screen.active_bindings` whose `type(node).__module__` starts with `"tdb."` has a non-empty `binding.description`.
    - `test_k_closes_key_help_and_survives_other_modals` (Review Focus 1):
      - In the key help modal, `K` dismisses it.
      - `app.action_keybindings()` opens Configure > Keybindings. `K` there neither raises nor leaves the app.
    - `test_collect_with_nothing_focused` (Review Focus 2): call `app.screen.set_focus(None)`, then `collect_key_help(app.screen)`. `view == []`, `everywhere` is non-empty, and `title == "Keys"`.
    - `test_keybindings_modal_unchanged`: keep `tests/unit/test_keybindings_modal.py` passing. It is the guard for the `format_key_line` lift, so no new test is needed.

- [ ] **Step 2: Run to verify they fail**
  Run: `uv run --extra dev pytest tests/unit/test_key_help.py -q --no-cov -p no:cacheprovider`
  Expected: an ImportError on `tdb.widgets.key_help`, or the assertions fail.

- [ ] **Step 3: Implement**, per the Interfaces block.
  - **Missing descriptions.** Add them to the tdb bindings that `test_every_tdb_binding_has_a_description` names. Keep the wording short, in the style of `"Enable/Disable"`.
  - **Docs.** In `docs/using/keybindings.md`, next to the "Configure > Keybindings" line, add: `Press K in any view (except the Code View's Edit mode) to see every key that view accepts.`

- [ ] **Step 4: Run to verify they pass**
  Run: `uv run --extra dev pytest tests/unit/test_key_help.py tests/unit/test_keybindings_modal.py tests/unit/test_keybindings.py -q --no-cov -p no:cacheprovider`
  Expected: all pass.

- [ ] **Step 5: Commit** (`Add K: the keys for the current view`)

---

### Task 2: Doc search result keys and footer

**Files:**
- Modify: `src/tdb/widgets/searchable_markdown.py`
- Modify: `src/tdb/widgets/modals.py` (`_DocumentationModal` footer swap)
- Modify: `docs/using/keybindings.md:45-49`, `docs/reference/cli.md:51`
- Test: `tests/unit/test_searchable_markdown.py` (append)

**Interfaces:**
- **New in `searchable_markdown.py`:**
  - `RESULTS_HINT = "n/→ next · p/←/N previous · q exit search"`.
  - `SearchableMarkdownViewer.results_mode -> bool` (property), equal to `self.search_active and self.document.has_focus`.
  - `SearchableMarkdownViewer.ResultsModeChanged(Message)`, with `active: bool`.
    - It is posted whenever `results_mode` flips.
    - Check after `action_open_search`, `action_close_search` and `on_input_submitted`, and on descendant focus or blur.
    - When it flips, the viewer also calls `self.refresh_bindings()`, so `tdb --doc`'s `Footer` re-reads `check_action`.
- **On `_SearchableMarkdown`, the focused document**, the bindings below. Define `_NEXT = Binding.Group("next")` and `_PREV = Binding.Group("previous")`. Each action needs its own name, because Textual's `Footer` shows only one key per action.
  - `Binding("n", "next_match", "next", group=_NEXT)`
  - `Binding("right", "next_match_arrow", "next", group=_NEXT)`
  - `Binding("p", "prev_match_p", "previous", group=_PREV)`
  - `Binding("left", "prev_match_arrow", "previous", group=_PREV)`
  - `Binding("N", "prev_match", "previous", group=_PREV)`
  - `Binding("q", "exit_search", "exit search")`

  Its `check_action` returns `True` for those six actions only when the ancestor viewer's `results_mode` is true. Otherwise it returns `False`, so the key falls through to the viewer's scroll bindings or to the host's `q`. The actions call the viewer's `_step(1)`, `_step(-1)` or `action_close_search()`.
- **Remove `n` and `N` from `SearchableMarkdownViewer.BINDINGS`**; they now live on the document. `slash`, `ctrl+f` and `escape` stay.
- **`_DocumentationModal.on_searchable_markdown_viewer_results_mode_changed`** sets `#doc-footer` to `f"[dim]{RESULTS_HINT}[/dim]"` while active. Otherwise it restores today's `f"[dim]{SEARCH_HINT} · ESC or q to close[/dim]"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_searchable_markdown.py`, reusing `_Host`, `_press`, `_viewer`, `tdb_app` and `DOC`)
  - `test_results_keys_step_after_enter`:
    - Type `slash`, then `gdb`, then `enter`. `current_index == 0`: `gdb` is below the incremental threshold, so Enter runs a fresh search.
    - `right` adds 1, `left` subtracts 1, `p` subtracts 1 with wrap-around, `N` subtracts 1, and `n` adds 1.
  - `test_q_after_enter_exits_search_not_viewer`: after `slash`, `gdb`, `enter`, pressing `q` makes `search_active` false while `quit_called` stays false. A second `q` sets `quit_called`.
  - `test_q_after_enter_with_no_matches_exits_search` (Review Focus 4): `slash`, `zzzz`, `enter`, then `q` makes `search_active` false and `quit_called` stays false.
  - `test_arrows_scroll_when_not_searching` (Review Focus 5):
    - With no search, `app.screen.active_bindings["right"].binding.action == "scroll_right"` and its `node` is the viewer. The same holds for `"left"`.
    - After `slash`, `gdb`, `enter`, `"right"` resolves to `"next_match_arrow"`.
  - `test_doc_app_footer_shows_results_keys`: use `make_doc_app(DOC)`.
    - Before searching, `active_bindings["q"].binding.action == "quit"`.
    - After `slash`, `gdb`, `enter`:
      - `active_bindings` has `n`, `right`, `p`, `left` and `N`, each with `show` set and the `"next"` or `"previous"` group;
      - `q` has action `"exit_search"` and description `"exit search"`.
    - After `escape`, `q` is back to `"quit"`.
  - `test_documentation_modal_footer_switches_after_enter`: uses `tdb_app` and `app.action_documentation()`.
    - After `slash`, `gdb`, `enter`, `#doc-footer` contains `RESULTS_HINT`.
    - After `escape`, it contains `SEARCH_HINT` and `"ESC or q to close"` again.
  - Existing tests stay green, notably `test_n_and_shift_n_wrap`, `test_n_without_search_is_noop`, `test_escape_after_enter_closes_bar_not_viewer`, `test_doc_app_uses_searchable_viewer` and `test_documentation_modal_search_and_q`.

- [ ] **Step 2: Run to verify they fail**
  Run: `uv run --extra dev pytest tests/unit/test_searchable_markdown.py -q --no-cov -p no:cacheprovider`
  Expected: the new tests fail; for example `RESULTS_HINT` fails to import, or `q` quits.

- [ ] **Step 3: Implement**, per the Interfaces block.
  - **`docs/using/keybindings.md`.** Replace "`n`/`N` step forward/back, `Esc` closes the search bar." with:

    > "After `Enter`, `n` or → steps forward, `p`, ← or `N` steps back, and `q` or `Esc` leaves search; the footer lists these keys."
  - **`docs/reference/cli.md`, the `--doc` row.** Make the search part read:

    > "`/` or `Ctrl+F` searches; after `Enter`, `n`/→ and `p`/←/`N` step through matches and `q` leaves search"

- [ ] **Step 4: Run to verify they pass**
  Run: `uv run --extra dev pytest tests/unit/test_searchable_markdown.py tests/unit/test_documentation.py -q --no-cov -p no:cacheprovider`
  Expected: all pass.

- [ ] **Step 5: Full suite.** Run `memcap-pytest -q -n 8 --no-cov -p no:cacheprovider` from the worktree.
  - Known failures, not regressions:
    - the 2 xdist-only `tests/unit/test_wait_graph.py` recursion-limit tests;
    - `test_console_stdin::test_exited_disables_input` under load, which passes alone.
  - Kill leftover `rust-concurrency-fixture park` processes afterwards.

- [ ] **Step 6: Commit** (`Doc search: n/→, p/←/N and q after Enter, with footer hints`)
