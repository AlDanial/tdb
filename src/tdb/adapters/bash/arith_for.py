"""Work around bash's nested arithmetic-for line-number bug.

bash's parser keeps ONE global (`arith_for_lineno` in parse.y) that is
overwritten every time a `for ((` header is read, and only consumed when
the whole `for ((...)); do ... done` command is reduced. An outer
arithmetic for loop whose body contains another arithmetic for loop is
therefore reduced *after* the inner header overwrote the global, so every
command bash synthesises for the outer header -- its init, test and step
expressions, which is what the DEBUG trap fires on -- carries the line
number of the lexically LAST `for ((` header inside its body. All bash
versions through 5.3 behave this way; the DEBUG trap sees it as $LINENO.

In pi_digits-style code:

    24  for ((step=0; step<=count; step++)); do
    25      q=0
    26      for ((i=size; i>=1; i--)); do

bash reports `((step=0))`, `((step<=count))` and `((step++))` at line 26,
so a breakpoint on line 24 can never hit and stepping highlights 26.

The harness can't see the source, but it does see $BASH_COMMAND, which
for an arithmetic-for expression is "((" + the expression + "))" (leading
whitespace stripped, $-expansions already performed). So this module
scans the script once, finds every mis-attributed loop, and produces a
glob pattern per expression; the harness (`fixup` command, see
tdb_harness.sh) remaps a stop at (file, reported line) whose
$BASH_COMMAND matches the pattern to the loop's true line.

The scanner is deliberately approximate -- comments, quotes, heredocs
and `((...))` are skipped, loop keywords are only honoured at command
position -- and fails soft: anything it can't pair up simply yields no
fixup, leaving bash's own line number in place.
"""

from __future__ import annotations

import re

_LOOP_OPENERS = frozenset({"for", "while", "until", "select"})
# words after which the next word is at command position
_CMD_POS_WORDS = frozenset(
    {"then", "do", "else", "elif", "if", "!", "time", "{", "}", "(", ")"}
)
_IDENT = re.compile(r"[A-Za-z0-9_]+|[#?@*$!0-9-]")


def expr_pattern(expr: str) -> str:
    """Glob pattern (for bash `[[ == ]]`) matching bash's rendering of one
    arithmetic-for expression in $BASH_COMMAND, minus the `((`/`))`.

    bash strips leading whitespace, runs an empty expression as `1`, and
    expands `$var`/`${...}`/`$(...)`/`$((...))`/backticks before the
    DEBUG trap sees the text, so those become `*`; every other non-word
    character is backslash-escaped so it matches literally even when the
    debuggee has `extglob` on. Whitespace runs also become `*` (bash keeps
    trailing/interior whitespace verbatim, but a newline inside the
    header would break the line-oriented wire format).
    """
    expr = expr.lstrip()
    if not expr:
        return "1"
    out: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        c = expr[i]
        if c == "$" or c == "`":
            i = _skip_expansion(expr, i)
            if out and out[-1] == "*":
                continue
            out.append("*")
            continue
        if c.isspace():
            while i < n and expr[i].isspace():
                i += 1
            if not (out and out[-1] == "*"):
                out.append("*")
            continue
        out.append(c if c.isalnum() or c == "_" else "\\" + c)
        i += 1
    return "".join(out)


def _skip_expansion(s: str, i: int) -> int:
    """Index just past the `$...`/backtick expansion starting at s[i]."""
    if s[i] == "`":
        j = s.find("`", i + 1)
        return len(s) if j < 0 else j + 1
    j = i + 1
    if j >= len(s):
        return j
    if s[j] == "{":
        return _skip_balanced(s, j, "{", "}")
    if s[j] == "(":
        return _skip_balanced(s, j, "(", ")")
    m = _IDENT.match(s, j)
    return m.end() if m else j


def _skip_balanced(s: str, i: int, open_: str, close: str) -> int:
    depth = 0
    while i < len(s):
        if s[i] == open_:
            depth += 1
        elif s[i] == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(s)


# ---- tokenizer -------------------------------------------------------------

_WORD_BREAK = frozenset(" \t\n;&|(){}<>")


def _tokens(text: str):
    """Yield (kind, value, line): kind is "word", "op", "nl" or "arith"
    (value = text between `((` and `))`). Comments, quotes, heredoc bodies
    and `$(...)`/`$((...))`/`${...}` bodies are skipped."""
    i = 0
    n = len(text)
    line = 1
    pending_heredocs: list[tuple[str, bool]] = []
    while i < n:
        c = text[i]
        if c == "\n":
            yield ("nl", "\n", line)
            line += 1
            i += 1
            if pending_heredocs:
                for term, strip_tabs in pending_heredocs:
                    while i < n:
                        end = text.find("\n", i)
                        if end < 0:
                            end = n
                        body_line = text[i:end]
                        if strip_tabs:
                            body_line = body_line.lstrip("\t")
                        i = end + 1
                        line += 1
                        if body_line == term:
                            break
                pending_heredocs = []
            continue
        if c in " \t":
            i += 1
            continue
        if c == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                line += 1
            i += 2
            continue
        if c == "#":
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if c == "(" and text.startswith("((", i):
            j = _skip_balanced(text, i, "(", ")")
            # j is past the first `)` of `))`; bash requires `))` to close
            if text.startswith(")", j):
                j += 1
            body = text[i + 2 : j - 2]
            yield ("arith", body, line)
            line += body.count("\n")
            i = j
            continue
        if text.startswith("<<", i) and not text.startswith("<<<", i):
            j = i + 2
            strip_tabs = False
            if j < n and text[j] == "-":
                strip_tabs = True
                j += 1
            while j < n and text[j] in " \t":
                j += 1
            k = j
            term_chars: list[str] = []
            while k < n and text[k] not in _WORD_BREAK:
                if text[k] in "'\"":
                    q = text.find(text[k], k + 1)
                    if q < 0:
                        q = n - 1
                    term_chars.append(text[k + 1 : q])
                    k = q + 1
                else:
                    term_chars.append(text[k])
                    k += 1
            pending_heredocs.append(("".join(term_chars), strip_tabs))
            i = k
            continue
        if c in ";&|":
            j = i + 1
            while j < n and text[j] in ";&|":
                j += 1
            yield ("op", text[i:j], line)
            i = j
            continue
        if c in "(){}<>":
            yield ("op", c, line)
            i += 1
            continue
        # a word: runs until an unquoted break char
        j = i
        while j < n and text[j] not in _WORD_BREAK:
            ch = text[j]
            if ch == "\\":
                if j + 1 < n and text[j + 1] == "\n":
                    line += 1
                j += 2
            elif ch == "'":
                q = text.find("'", j + 1)
                if q < 0:
                    q = n - 1
                line += text.count("\n", j, q)
                j = q + 1
            elif ch == '"':
                j = _skip_dquote(text, j)
                line += text.count("\n", i, j) - text.count("\n", i, i)
            elif ch == "$" and j + 1 < n and text[j + 1] in "({":
                k = _skip_expansion(text, j)
                line += text.count("\n", j, k)
                j = k
            elif ch == "`":
                q = text.find("`", j + 1)
                if q < 0:
                    q = n - 1
                line += text.count("\n", j, q)
                j = q + 1
            else:
                j += 1
        yield ("word", text[i:j], line)
        i = j


def _skip_dquote(s: str, i: int) -> int:
    i += 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        elif c == "$" and i + 1 < n and s[i + 1] in "({":
            i = _skip_expansion(s, i)
        else:
            i += 1
    return n


# ---- loop pairing ----------------------------------------------------------


class _Loop:
    __slots__ = ("line", "exprs", "last_arith")

    def __init__(self, line: int, exprs: str | None) -> None:
        self.line = line
        self.exprs = exprs  # None for for-in / while / until / select
        self.last_arith: int | None = None  # bash's arith_for_lineno at `done`


def arith_for_fixups(text: str) -> dict[int, list[tuple[int, str]]]:
    """Map each line bash will mis-report -> ordered [(true line, pattern)].

    Every arithmetic for loop whose body contains another one (at a later
    line) is reported by bash at the line of the last nested header. For
    each such reported line R the list holds, first, the patterns of the
    loop literally at R, then those of each mis-attributed loop,
    innermost first; the harness takes the first pattern that matches
    $BASH_COMMAND. Patterns include the `((`/`))` bash prints.
    """
    stack: list[_Loop] = []
    headers: dict[int, str] = {}  # true line -> raw exprs, all arith loops
    misreported: dict[int, list[tuple[int, str]]] = {}
    cmd_pos = True
    pending_for: int | None = None  # line of a `for` whose kind is unknown yet
    for kind, value, line in _tokens(text):
        if pending_for is not None:
            if kind == "arith":
                loop = _Loop(pending_for, value)
                for enclosing in stack:
                    enclosing.last_arith = loop.line
                headers[loop.line] = value
            else:
                loop = _Loop(pending_for, None)  # `for x in ...`
            stack.append(loop)
            pending_for = None
            cmd_pos = False
            continue
        if kind == "word":
            if cmd_pos and value == "for":
                pending_for = line
            elif cmd_pos and value in _LOOP_OPENERS:
                stack.append(_Loop(line, None))
            elif cmd_pos and value == "done" and stack:
                loop = stack.pop()
                if (
                    loop.exprs is not None
                    and loop.last_arith is not None
                    and loop.last_arith != loop.line
                ):
                    misreported.setdefault(loop.last_arith, []).append(
                        (loop.line, loop.exprs)
                    )
            cmd_pos = value in _CMD_POS_WORDS
        else:  # op / nl / stray arith command
            cmd_pos = kind != "arith"
    result: dict[int, list[tuple[int, str]]] = {}
    for reported in sorted(misreported):
        entries: list[tuple[int, str]] = []
        if reported in headers:
            entries.extend(_patterns(reported, headers[reported]))
        for true_line, exprs in misreported[reported]:
            entries.extend(_patterns(true_line, exprs))
        result[reported] = entries
    return result


def _patterns(line: int, exprs: str) -> list[tuple[int, str]]:
    parts = exprs.split(";")
    if len(parts) != 3:
        return []
    return [(line, "\\(\\(" + expr_pattern(p) + "\\)\\)") for p in parts]
