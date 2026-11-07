# Source navigation

## Intent and agreed behavior

Add a Source action immediately to the right of the existing File action
(called Files in the request). Clicking Source or pressing Alt-S opens a
modal tree of files relevant to the debugged program. All directories start
expanded; clicking a directory toggles its branch. Up/down and j/k move the
selection, Enter opens a file in Code View, and Escape cancels.

Selecting a source file keeps the debug target, execution state, and selected
stack frame unchanged. Existing breakpoint display and unsaved-edit safeguards
must apply when changing the displayed file.

The user chose static Python import discovery. `--source=FILE` adds explicit
filenames to the discovered set rather than replacing it.

## Approaches

Static analysis follows imports without executing the debuggee and works before
the program runs. Runtime discovery could capture dynamic imports, but only
after execution and with additional debugger integration. The user selected
static discovery; runtime discovery is out of scope.

## Proposed implementation defaults

- Include the entry script when it is an existing source file. For Python,
  parse imports recursively using the AST, with cycle detection. Resolve
  absolute imports against the entry script directory and the debuggee working
  directory; resolve relative imports against their containing package.
- Follow exact module names, including aliases, dotted imports, packages and
  `from package import module` forms. Include relevant package `__init__.py`
  files. Support local namespace-package directories. Do not execute imports,
  search installed libraries or PYTHONPATH, or guess misspelled module names.
  Unresolved imports are omitted. Dynamic imports and star-import expansion
  beyond the named module are out of scope.
- Files that cannot be parsed remain selectable, but yield no further imports.
  Discovery uses saved files on disk and refreshes each time the modal opens.
- The list file is UTF-8, one path per line. Ignore content from the first `#`
  through the end of each line, trim whitespace, and skip blank lines. Relative
  paths resolve against the list file's directory; absolute paths are accepted.
  Listed paths are literal filenames, with no glob or shell expansion. Existing
  files of any language may be listed. Deduplicate resolved paths. Listed Python
  files are additional discovery roots for Python sessions.
- An unreadable list file produces a clear CLI error. Missing or unreadable
  listed source files are skipped with a warning; valid entries still work.
- Outside Python sessions, show explicitly listed files and an available source
  entry file, without attempting language-specific dependency discovery or
  presenting native executable binaries as source.
- Build the tree from the finite collected file set, rather than scanning all
  directory contents. Sort directories and files deterministically. Use the
  working directory as the root when all files fit beneath it; otherwise use
  their common ancestor, labeled with its path. Show an empty-state message
  when no local source files are available.
- Match the existing file modal's dimensions and styling, using a dedicated
  tree populated eagerly so every branch starts expanded. Preserve the current
  File action's label and behavior.

## Integration and validation

Keep discovery and list parsing separate from the UI. Wire the CLI option
through app construction and applicable run/attach entry paths. Source
navigation should work without restart support whenever local files exist.

Test import resolution, recursion and cycles, package and relative imports,
unresolved modules, malformed source, additive list handling, duplicates and
external paths. Test CLI parsing and option propagation. Exercise the modal
with Textual tests covering initial expansion, collapse, keyboard navigation,
selection and cancellation; verify selection updates Code View without
restarting the session. Document the option, path semantics, shortcut, and
static-discovery limitations. Run the relevant existing UI and CLI regressions.

This is a bounded addition to the existing CLI and Textual interface. Detailed
class names and internal wiring remain implementation choices.
