"""Parse `declare -p` / `local -p` output into (name, value, children).

Input lines look like:
  declare -- s="text with \\" escapes"
  declare -i n="5"
  declare -a arr=([0]="x" [1]="y")
  declare -A map=([k]="v")
  declare -- flag              (no value; `local -p` emits these)
Anything that doesn't match a declare line is skipped. Values keep
bash's own double-quoted rendering verbatim (the UI shows them as-is);
only the quoting *structure* is parsed, never unescaped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DECLARE_RE = re.compile(
    r"^declare\s+-([-aAilnrtux]+)\s+([A-Za-z_][A-Za-z0-9_]*)(=(.*))?$"
)


@dataclass(frozen=True)
class BashVar:
    name: str
    value: str
    children: list[tuple[str, str]] | None


def _scan_quoted(text: str, i: int) -> int:
    """Given text[i] == '\"', return index just past the closing quote."""
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return len(text)


def _parse_array_items(body: str) -> list[tuple[str, str]]:
    """body is the text inside (...) of an array literal."""
    items: list[tuple[str, str]] = []
    i = 0
    n = len(body)
    while i < n:
        while i < n and body[i] in " \t":
            i += 1
        if i >= n or body[i] != "[":
            break
        close = body.index("]=", i)
        key = body[i + 1 : close]
        i = close + 2
        if i < n and body[i] == '"':
            end = _scan_quoted(body, i)
        else:
            end = i
            while end < n and body[end] not in " \t":
                end += 1
        items.append((key, body[i:end]))
        i = end
    return items


def parse_declares(text: str) -> list[BashVar]:
    out: list[BashVar] = []
    for line in text.splitlines():
        m = _DECLARE_RE.match(line.strip())
        if m is None:
            continue
        flags, name, _, value = m.groups()
        value = value if value is not None else ""
        if ("a" in flags or "A" in flags) and value.startswith("("):
            items = _parse_array_items(value[1 : value.rfind(")")])
            if "A" in flags:
                items.sort(key=lambda kv: kv[0])
                summary = f"assoc[{len(items)}]"
            else:
                items.sort(key=lambda kv: int(kv[0]))
                summary = f"array[{len(items)}]"
            out.append(BashVar(name=name, value=summary, children=items))
        else:
            out.append(BashVar(name=name, value=value, children=None))
    return out
