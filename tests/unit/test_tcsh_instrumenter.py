import shutil
import subprocess
from pathlib import Path

import pytest

from tdb.adapters.tcsh.instrumenter import Instrumenter

TCSH = shutil.which("tcsh")


def make_instrumenter(workspace: Path, cwd: Path | None = None) -> Instrumenter:
    return Instrumenter(
        workspace=workspace,
        cwd=cwd or workspace.parent,
        original_argv0="debugged.csh",
        probe_renderer=lambda probe_id: f"# probe {probe_id}\n",
        source_event_renderer=lambda event, depth: f"# source-{event} {depth}\n",
    )


def decode_literal_tcsh_word(word: str) -> str:
    """Decode the conservative tcsh word syntax emitted by the instrumenter."""

    if word == "''":
        return ""
    decoded: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(word):
        character = word[index]
        if character == "\\":
            index += 1
            assert index < len(word), "trailing backslash is not a literal tcsh word"
            decoded.append(word[index])
        elif character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            else:
                decoded.append(character)
        else:
            assert not (character in "$`" and quote != "'")
            assert character != "!"
            assert not (character.isspace() and quote is None)
            decoded.append(character)
        index += 1
    assert quote is None
    return "".join(decoded)


def source_operands(generated: str) -> list[str]:
    return [line.removeprefix("source ") for line in generated.splitlines() if line.startswith("source ")]


def test_instrumenter_inserts_probe_before_command(tmp_path: Path) -> None:
    program = tmp_path / "main.csh"
    program.write_text("#!/bin/tcsh\necho hello\n")
    workspace = tmp_path / "session"
    workspace.mkdir()

    result = make_instrumenter(workspace).instrument(program)

    generated = result.root.read_text()
    assert generated.startswith("#!/bin/tcsh\n")
    assert "source " in generated
    assert generated.endswith("echo hello\n")
    assert result.source_map.probes[0].span.start_line == 2
    assert (workspace / "probes" / "1.csh").read_text() == "# probe 1\n"


def test_root_without_shebang_keeps_its_first_command_probeable(tmp_path: Path) -> None:
    program = tmp_path / "main.csh"
    program.write_text("echo hello\n")
    workspace = tmp_path / "session"
    workspace.mkdir()

    result = make_instrumenter(workspace).instrument(program)

    assert [probe.span.start_line for probe in result.source_map.probes] == [1]
    assert result.root.read_text().endswith("echo hello\n")


def test_literal_source_is_recursively_instrumented_in_depth_first_order(tmp_path: Path) -> None:
    library = tmp_path / "lib.csh"
    library.write_text("echo library\n")
    main = tmp_path / "main.csh"
    main.write_text("source lib.csh\necho root\n")
    workspace = tmp_path / "session"
    workspace.mkdir()

    result = make_instrumenter(workspace, cwd=tmp_path).instrument(main)

    main_path = main.resolve()
    library_path = library.resolve()
    generated_library = result.generated_by_original[library_path]
    generated = result.root.read_text()
    assert set(result.generated_by_original) == {main_path, library_path}
    assert generated_library.read_text().endswith("echo library\n")
    assert str(generated_library) in [decode_literal_tcsh_word(word) for word in source_operands(generated)]
    assert "# source-enter 1\n" in generated
    assert "# source-leave 1\n" in generated
    assert [(probe.id, probe.span.path, probe.span.start_line, probe.source_depth) for probe in result.source_map.probes] == [
        (1, main_path, 1, 0),
        (2, library_path, 1, 1),
        (3, main_path, 2, 0),
    ]


def test_cycle_reuses_known_generated_path_without_recursing(tmp_path: Path) -> None:
    first = tmp_path / "first.csh"
    second = tmp_path / "second.csh"
    first.write_text("source second.csh\necho first\n")
    second.write_text("source first.csh\necho second\n")
    workspace = tmp_path / "session"
    workspace.mkdir()

    result = make_instrumenter(workspace, cwd=tmp_path).instrument(first)

    assert set(result.generated_by_original) == {first.resolve(), second.resolve()}
    assert result.root.exists()
    assert len(result.source_map.probes) == 4


def test_duplicate_sources_reuse_one_generated_file(tmp_path: Path) -> None:
    library = tmp_path / "lib.csh"
    library.write_text("echo library\n")
    main = tmp_path / "main.csh"
    main.write_text("source lib.csh\nsource lib.csh\n")
    workspace = tmp_path / "session"
    workspace.mkdir()

    result = make_instrumenter(workspace, cwd=tmp_path).instrument(main)

    generated_library = result.generated_by_original[library.resolve()]
    decoded_sources = [decode_literal_tcsh_word(word) for word in source_operands(result.root.read_text())]
    assert decoded_sources.count(str(generated_library)) == 2
    assert len(result.source_map.probes) == 3


def test_source_path_with_spaces_is_rewritten_as_a_quoted_absolute_path(tmp_path: Path) -> None:
    library = tmp_path / "library with spaces.csh"
    library.write_text("echo library\n")
    main = tmp_path / "main.csh"
    main.write_text('source "library with spaces.csh"\n')
    workspace = tmp_path / "session"
    workspace.mkdir()

    result = make_instrumenter(workspace, cwd=tmp_path).instrument(main)

    decoded_sources = [decode_literal_tcsh_word(word) for word in source_operands(result.root.read_text())]
    assert str(result.generated_by_original[library.resolve()]) in decoded_sources


def test_structural_and_opaque_units_receive_no_probes_and_label_probe_follows_label(
    tmp_path: Path,
) -> None:
    program = tmp_path / "main.csh"
    program.write_text("again:\nif (1) then\necho yes\nendif\nsource $dynamic\n")
    workspace = tmp_path / "session"
    workspace.mkdir()

    result = make_instrumenter(workspace).instrument(program)

    generated = result.root.read_text()
    first_probe_word = next(
        word
        for word in source_operands(generated)
        if decode_literal_tcsh_word(word) == str(workspace / "probes" / "1.csh")
    )
    first_probe_source = f"source {first_probe_word}\n"
    assert generated.index("again:\n") < generated.index(first_probe_source) < generated.index("if (1) then")
    assert [probe.span.start_line for probe in result.source_map.probes] == [1, 3]


def test_only_eligible_lexical_dollar_zero_is_rewritten(tmp_path: Path) -> None:
    program = tmp_path / "main.csh"
    program.write_text('echo $0 "$0" \'$0\' \\$0 # $0\ncat << EOF\n$0\nEOF\n')
    workspace = tmp_path / "session"
    workspace.mkdir()

    result = make_instrumenter(workspace).instrument(program)

    generated = result.root.read_text()
    assert 'echo ${__tcsh_dap_original_0} "${__tcsh_dap_original_0}" \'$0\' \\$0 # $0\n' in generated
    assert "cat << EOF\n$0\nEOF\n" in generated


def test_tcsh_words_quote_expansion_metacharacters_literally(tmp_path: Path) -> None:
    metacharacters = "space $d `cmd` !*?[]{}~%'\\\""
    source_dir = tmp_path / f"source {metacharacters}"
    source_dir.mkdir()
    program = source_dir / f"main {metacharacters}.csh"
    program.write_text("echo $0\n")
    workspace = tmp_path / f"session {metacharacters}"
    workspace.mkdir()
    argv0 = f"argv0 {metacharacters}"
    instrumenter = Instrumenter(
        workspace=workspace,
        cwd=source_dir,
        original_argv0=argv0,
        probe_renderer=lambda probe_id: f"# probe {probe_id}\n",
        source_event_renderer=lambda event, depth: f"# {event} {depth}\n",
    )

    result = instrumenter.instrument(program)

    generated = result.root.read_text()
    assignment = generated.splitlines()[0].removeprefix("set __tcsh_dap_original_0 = ")
    assert decode_literal_tcsh_word(assignment) == argv0
    assert [decode_literal_tcsh_word(word) for word in source_operands(generated)] == [
        str(workspace / "probes" / "1.csh")
    ]


@pytest.mark.parametrize("invalid", ["bad\nvalue", "bad\x00value"])
def test_original_argv0_rejects_nonrepresentable_characters(tmp_path: Path, invalid: str) -> None:
    program = tmp_path / "main.csh"
    program.write_text("echo hello\n")
    workspace = tmp_path / "session"
    workspace.mkdir()

    with pytest.raises(ValueError, match="NUL|newline"):
        Instrumenter(
            workspace=workspace,
            cwd=tmp_path,
            original_argv0=invalid,
            probe_renderer=lambda probe_id: f"# probe {probe_id}\n",
            source_event_renderer=lambda event, depth: f"# {event} {depth}\n",
        ).instrument(program)


def test_generated_path_rejects_newline(tmp_path: Path) -> None:
    program = tmp_path / "main.csh"
    program.write_text("echo hello\n")
    workspace = tmp_path / "session\nunsafe"
    workspace.mkdir()

    with pytest.raises(ValueError, match="newline"):
        make_instrumenter(workspace, cwd=tmp_path).instrument(program)


def test_continued_heredoc_header_preserves_body_dollar_zero(tmp_path: Path) -> None:
    program = tmp_path / "main.csh"
    program.write_text("cat \\\n<< EOF\n$0\nEOF\n")
    workspace = tmp_path / "session"
    workspace.mkdir()

    result = make_instrumenter(workspace).instrument(program)

    assert "cat \\\n<< EOF\n$0\nEOF\n" in result.root.read_text()


def test_final_label_is_separated_from_inserted_probe(tmp_path: Path) -> None:
    program = tmp_path / "main.csh"
    program.write_text("again:")
    workspace = tmp_path / "session"
    workspace.mkdir()

    result = make_instrumenter(workspace).instrument(program)

    assert "again:\nsource " in result.root.read_text()


def test_final_literal_source_is_separated_from_leave_event(tmp_path: Path) -> None:
    library = tmp_path / "lib.csh"
    library.write_text("echo library")
    program = tmp_path / "main.csh"
    program.write_text("source lib.csh")
    workspace = tmp_path / "session"
    workspace.mkdir()

    result = make_instrumenter(workspace, cwd=tmp_path).instrument(program)

    generated_library = result.generated_by_original[library.resolve()]
    generated = result.root.read_text()
    assert "\n# source-leave 1\n" in generated
    assert decode_literal_tcsh_word(source_operands(generated)[1]) == str(generated_library)


def test_shebang_only_without_final_newline_is_valid(tmp_path: Path) -> None:
    program = tmp_path / "main.csh"
    program.write_text("#!/bin/tcsh")
    workspace = tmp_path / "session"
    workspace.mkdir()

    result = make_instrumenter(workspace).instrument(program)

    assert result.root.read_text() == '#!/bin/tcsh\nset __tcsh_dap_original_0 = debugged.csh\n'


@pytest.mark.skipif(TCSH is None, reason="stock tcsh is not installed")
def test_generated_script_executes_with_literal_paths_and_separated_fragments(tmp_path: Path) -> None:
    library = tmp_path / "lib.csh"
    library.write_text("echo library")
    program = tmp_path / "main $`! file.csh"
    program.write_text('echo "$0"\nsource lib.csh')
    workspace = tmp_path / "session $`! directory"
    workspace.mkdir()
    instrumenter = Instrumenter(
        workspace=workspace,
        cwd=tmp_path,
        original_argv0="literal $argv `command` ! value",
        probe_renderer=lambda probe_id: f"# probe {probe_id}\n",
        source_event_renderer=lambda event, depth: f"echo {event}-{depth}\n",
    )
    result = instrumenter.instrument(program)

    completed = subprocess.run(
        [TCSH, str(result.root)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.splitlines() == [
        "literal $argv `command` ! value",
        "enter-1",
        "library",
        "leave-1",
    ]
