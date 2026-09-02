"""Language registration and target detection.

Detection chain (first hit wins):
  1. caller-supplied --lang (handled upstream in cli.py; detect() is
     only called when --lang was not given)
  2. file extension (.py -> python, .go -> go, compiled-language source
     extensions -> actionable error)
  3. binary magic bytes (ELF / PE / Mach-O -> cpp)
  4. shebang mentioning python -> python
  5. no match -> error naming --lang
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from tdb.languages.base import LanguageNotSupportedError, LanguageProfile
from tdb.languages.python import build_python_profile

_BUILDERS: dict[str, Callable[..., LanguageProfile]] = {}


def register(lang_id: str, builder: Callable[..., LanguageProfile]) -> None:
    _BUILDERS[lang_id] = builder


def known_languages() -> list[str]:
    return sorted(_BUILDERS)


def resolve(
    lang_id: str,
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
) -> LanguageProfile:
    """Build the profile for a detected/requested language id.

    ``adapter_paths`` (TdbConfig.adapters: adapter id -> executable
    path) is forwarded to the builder, which resolves the override for
    whichever adapter it actually selects. ``program`` is forwarded
    too, for builders that need the debug target (e.g. OCaml's
    native/bytecode flavor); other builders ignore it.
    """
    builder = _BUILDERS.get(lang_id)
    if builder is None:
        raise LanguageNotSupportedError(
            f"language '{lang_id}' is not supported yet "
            f"(supported: {', '.join(known_languages())})"
        )
    return builder(adapter=adapter, adapter_paths=adapter_paths, program=program)


_EXTENSION_MAP = {
    ".py": "python",
    ".pyw": "python",
    ".go": "go",
    ".pl": "perl",
    ".pm": "perl",
    ".t": "perl",
    ".rb": "ruby",
    ".sh": "bash",
    ".bash": "bash",
    ".csh": "tcsh",
    ".tcsh": "tcsh",
}

# Source files for compiled languages: debugging the source is a user
# error — you debug the built executable.
_COMPILED_SOURCE_EXTS = {".c", ".cc", ".cpp", ".cxx", ".c++", ".rs", ".ml", ".mli"}


def reject_compiled_source(program: str | None) -> None:
    """Reject source paths even with an explicitly selected language."""
    if program is None:
        return
    extension = Path(program).suffix.lower()
    if extension not in _COMPILED_SOURCE_EXTS:
        return
    if extension == ".rs":
        raise LanguageNotSupportedError(
            f"{program!r} is Rust source — build a debug executable "
            f"(e.g. `rustc -g {program}` or `cargo build`) and run "
            "`tdb --lang rust ./binary`"
        )
    if extension in (".ml", ".mli"):
        raise LanguageNotSupportedError(
            f"{program!r} is OCaml source — build it first (dune's dev "
            f"profile keeps debug info) and run "
            f"`tdb ./_build/default/.../main.exe`"
        )
    raise LanguageNotSupportedError(
        f"{program!r} is source for a compiled language — compile "
        "with debug info (e.g. `g++ -g -O0`) and run `tdb ./binary`"
    )


_MAGIC = [
    (b"\x7fELF", "cpp"),  # Linux
    (b"MZ", "cpp"),  # Windows PE
    (b"\xcf\xfa\xed\xfe", "cpp"),  # Mach-O 64-bit LE
    (b"\xce\xfa\xed\xfe", "cpp"),  # Mach-O 32-bit LE
    (b"\xca\xfe\xba\xbe", "cpp"),  # Mach-O universal
]


def detect(program: str | None) -> str:
    """Infer the language id from the debug target.

    Called only when --lang was not given. `None` (remote-attach mode,
    no local program) defaults to python — tdb's historical behavior.
    """
    if program is None:
        return "python"
    path = Path(program)
    if path.is_dir():
        if any(path.glob("*.go")):
            return "go"
        raise LanguageNotSupportedError(
            f"{program!r} is a directory with no .go files — tdb debugs "
            f"a program file, or a Go package directory"
        )
    reject_compiled_source(program)
    ext = path.suffix.lower()
    if ext in _EXTENSION_MAP:
        return _EXTENSION_MAP[ext]

    from tdb.languages.ocaml import ocaml_flavor  # lazy: avoid import cycle

    if ocaml_flavor(program) is not None:
        return "ocaml"
    head = b""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        pass
    for magic, lang_id in _MAGIC:
        if head.startswith(magic):
            from tdb.languages.go import is_go_binary  # lazy: import cycle

            if is_go_binary(str(path)):
                return "go"
            return lang_id
    if head.startswith(b"#!") and b"python" in head.splitlines()[0]:
        return "python"
    if head.startswith(b"#!") and b"perl" in head.splitlines()[0]:
        return "perl"
    if head.startswith(b"#!") and b"ruby" in head.splitlines()[0]:
        return "ruby"
    if head.startswith(b"#!") and b"bash" in head.splitlines()[0]:
        return "bash"
    # "csh" matches both #!/bin/csh and #!/bin/tcsh; checked after bash
    # (which never contains "csh") so bash shebangs keep winning.
    if head.startswith(b"#!") and b"csh" in head.splitlines()[0]:
        return "tcsh"
    raise LanguageNotSupportedError(
        f"cannot determine the language of {program!r} — pass --lang "
        f"(supported: {', '.join(known_languages())})"
    )


def extensions_for(lang_id: str) -> tuple[str, ...]:
    """Extensions mapped to `lang_id`, for UI file filters (File > Open).

    Empty for languages detected by other means (cpp: binary magic
    bytes) — callers treat empty as "show all files".
    """
    return tuple(sorted(ext for ext, lang in _EXTENSION_MAP.items() if lang == lang_id))


def matches_language(path: str, lang_id: str) -> bool:
    """True when `path` detects as `lang_id` (File > Open's
    same-language guard). Detection failure counts as a mismatch."""
    try:
        return detect(path) == lang_id
    except LanguageNotSupportedError:
        return False


register("python", build_python_profile)

from tdb.languages.cpp import build_cpp_profile  # noqa: E402

register("cpp", build_cpp_profile)

from tdb.languages.perl import build_perl_profile  # noqa: E402

register("perl", build_perl_profile)

from tdb.languages.bash import build_bash_profile  # noqa: E402

register("bash", build_bash_profile)

from tdb.languages.tcsh import build_tcsh_profile  # noqa: E402

register("tcsh", build_tcsh_profile)

from tdb.languages.ruby import build_ruby_profile  # noqa: E402

register("ruby", build_ruby_profile)

from tdb.languages.ocaml import build_ocaml_profile  # noqa: E402

register("ocaml", build_ocaml_profile)

from tdb.languages.rust import build_rust_profile  # noqa: E402

register("rust", build_rust_profile)

from tdb.languages.go import build_go_profile  # noqa: E402

register("go", build_go_profile)
