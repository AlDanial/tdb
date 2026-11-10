# Source modal: dynamic resolution and curated lists

## Intent and agreed scope

Make it easier to navigate files used by the debugged program. Keep the existing
static search and add three Source modal actions: `d` resolves statically found
module names through Python search paths, `x` removes the highlighted file from
the list, and `s` saves the displayed list.

Use the selected debuggee interpreter's search paths, without importing discovered
modules. Stop after resolving the static import list: do not parse imports in
newly located modules. Ignore nonexistent search directories. Exclude standard
library files unless `--no-just-my-code` is set.

## Approaches

We considered resolving only the existing static import list and recursively
following imports in newly located files. The user selected the former: it keeps
the results focused and avoids expanding into an entire dependency graph.
Runtime module enumeration is outside scope; discovery must work before execution.

## Proposed implementation defaults

- Retain import requests, including unresolved names and relative-import context,
  from files examined during the existing static search. Keep the current static
  discovery API compatible for existing callers.
- Obtain search-path and standard-library metadata in a short-lived subprocess
  using the selected Python executable (the existing default when none is set).
  This describes the interpreter's startup environment, not subsequent changes
  made by a running debuggee to `sys.path`. Do not execute the target program or
  import discovered packages. Preserve interpreter startup configuration while
  keeping the metadata helper's own imports isolated from project modules.
- Resolve names by filesystem inspection in search-path order, preserving package
  boundaries and namespace-package behavior. Include Python source modules and
  relevant `__init__.py` files. Skip built-ins, native extensions, and archive
  entries that do not provide ordinary navigable source files. Keep third-party
  Python sources eligible even when standard-library sources are excluded.
- Merge dynamic results into the existing list, deduplicate absolute paths, and
  retain program-first ordering followed by sorted paths. Run discovery off the
  UI thread; show progress and report failures without losing the current list.
- `x` removes only a list entry; it never deletes a file. Keep the cursor on the
  next entry, or the previous entry when removing the last row. An empty list is
  valid and remains usable for discovery and saving.
- Keep discovered files and removals for the current app session so opening a
  file and reopening Source does not discard curation. Repeating `d` uses the
  original static import requests and respects session removals. A new app
  session starts with the normal static search and any `--source` entries.
- `s` saves directly to the filename supplied with `--source FILE`, or the last
  successfully chosen save filename. If neither exists, prompt for a filename;
  Escape cancels. Save UTF-8 absolute paths, one per line, using the existing
  source-list format. Report write errors and leave the modal open. Replace the
  destination atomically to avoid truncating an existing list on write failure.
- Preserve existing source-list loading semantics: `--source` supplements static
  discovery. Saving a reduced list does not introduce a persistent exclusion file.
- For non-Python sessions, retain `x` and `s`; make `d` unavailable with a clear
  explanation. Remote attach uses locally available interpreter metadata and
  files; querying a remote runtime is outside scope.

## Integration and validation

Propagate the source-list filename alongside its parsed entries through TUI
construction paths. Preserve ordinary navigation, file opening, and unsaved-edit
safeguards. Update the modal's shortcut hints and user documentation.

Test interpreter selection, missing path entries, exact module resolution,
package boundaries, standard-library filtering, and absence of recursive or
import-time execution. Test modal discovery, deletion and cursor behavior,
session persistence, save prompts and errors, source-list round trips, and CLI
filename propagation. Run relevant discovery, navigation, and CLI regressions.

This is a bounded extension of the existing Python discovery and Textual modal.
Internal class and helper names remain implementation choices.
