"""Pure helpers behind Code View Edit mode.

No Textual imports here: everything is unit-testable without a TUI.

- atomic_write_text: write-to-temp + os.replace so a crash mid-save
  never leaves a half-written source file (works on Windows because
  the temp file lives in the target's own directory).
- remap_line_numbers / remap_breakpoints: after a save, breakpoints
  set on the old text need new line numbers. difflib gives us the
  equal/insert/delete/replace blocks; we map through them.
- resolve_external_editor: $VISUAL / $EDITOR lookup for the
  "Open in $EDITOR" menu item.
"""

from __future__ import annotations

import difflib
import os
import shlex
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence

from tdb.dap.types import SourceBreakpoint


def atomic_write_text(path: str, text: str) -> None:
    """Write `text` (UTF-8) to `path` atomically. Raises OSError.

    `path` is resolved to its real target first: `os.replace` on a
    symlink would replace the link itself with a regular file, leaving
    the real file untouched. The temp file is created next to the
    resolved target (so `os.replace` stays on one filesystem) and its
    mode is copied from the existing target when there is one, so a
    `chmod +x` script doesn't lose its executable bit to umask on save.
    """
    target = os.path.realpath(path)
    directory = os.path.dirname(target) or "."
    name = os.path.basename(target)
    tmp = os.path.join(directory, f".{name}.tdb-tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            shutil.copymode(target, tmp)
        except OSError:
            pass  # target doesn't exist yet (new file): keep umask mode
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def remap_line_numbers(
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    lines: Iterable[int],
) -> dict[int, int | None]:
    """Map 1-based line numbers in `old_lines` to their position in
    `new_lines`. A line that was deleted maps to None.

    Rules (from the spec): lines inside an `equal` block map by
    offset; inside a `replace` block they map by offset when the old
    and new blocks have the same size, otherwise to the first line of
    the new block; inside a `delete` block they map to None.
    """
    wanted = set(lines)
    result: dict[int, int | None] = {}
    matcher = difflib.SequenceMatcher(None, list(old_lines), list(new_lines))
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        for old_idx in range(i1, i2):
            line = old_idx + 1
            if line not in wanted:
                continue
            if tag == "equal" or (tag == "replace" and (i2 - i1) == (j2 - j1)):
                result[line] = j1 + (old_idx - i1) + 1
            elif tag == "replace":
                result[line] = j1 + 1
            else:  # delete
                result[line] = None
    for line in wanted:
        result.setdefault(line, None)
    return result


def remap_breakpoints(
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    bps: list[SourceBreakpoint],
) -> list[SourceBreakpoint]:
    """Return breakpoints with lines remapped across the edit.

    Deleted lines drop their breakpoint. When two breakpoints land on
    the same new line, the first (in input order) wins so its
    condition / enabled flag survive.
    """
    mapping = remap_line_numbers(old_lines, new_lines, [bp.line for bp in bps])
    seen: set[int] = set()
    out: list[SourceBreakpoint] = []
    for bp in bps:
        new_line = mapping.get(bp.line)
        if new_line is None or new_line in seen:
            continue
        seen.add(new_line)
        out.append(
            SourceBreakpoint(
                line=new_line,
                condition=bp.condition,
                hit_condition=bp.hit_condition,
                log_message=bp.log_message,
                enabled=bp.enabled,
                persist=bp.persist,
            )
        )
    return out


def resolve_external_editor(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> list[str]:
    """Command (argv prefix) for the user's editor: $VISUAL, then
    $EDITOR, then a platform default. POSIX values are shlex-split so
    `EDITOR="code -w"` works; Windows values are passed verbatim."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    windows = platform.startswith("win")
    for var in ("VISUAL", "EDITOR"):
        value = env.get(var, "").strip()
        if value:
            return [value] if windows else shlex.split(value)
    return ["notepad"] if windows else ["vi"]
