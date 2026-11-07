import pytest

from tdb.source_discovery import discover_sources, read_source_list


def write(root, name, content=""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_recursive_local_imports_and_exact_names(tmp_path):
    main = write(
        tmp_path,
        "main.py",
        "import sys, os\nimport channels as Ch\nimport color.finder.utils\nfrom depth.dim import two, three\n",
    )
    write(tmp_path, "channels.py", "import main\n")
    write(tmp_path, "color/finder/utils.py")
    write(tmp_path, "color/finder/util.py")
    write(tmp_path, "depth/dim/two.py", "from . import three\n")
    write(tmp_path, "depth/dim/three.py")
    write(tmp_path, "unrelated.py")
    assert {str(p.relative_to(tmp_path)) for p in discover_sources(str(main))} == {
        "main.py",
        "channels.py",
        "color/finder/utils.py",
        "depth/dim/two.py",
        "depth/dim/three.py",
    }


def test_packages_initializers_relative_imports_and_star(tmp_path):
    main = write(
        tmp_path, "main.py", "from pkg.sub.mod import value\nfrom pkg.extra import *\n"
    )
    for name, code in {
        "pkg/__init__.py": "from . import init_dep\n",
        "pkg/init_dep.py": "",
        "pkg/sub/__init__.py": "",
        "pkg/sub/mod.py": "from .. import sibling\nvalue = 1\n",
        "pkg/sibling.py": "",
        "pkg/extra.py": "",
    }.items():
        write(tmp_path, name, code)
    assert len(discover_sources(str(main))) == 7


def test_searches_script_directory_and_cwd_without_pythonpath(tmp_path, monkeypatch):
    main = write(tmp_path, "scripts/main.py", "import nearby, working, elsewhere\n")
    nearby = write(tmp_path, "scripts/nearby.py")
    working = write(tmp_path, "cwd/working.py")
    write(tmp_path, "external/elsewhere.py")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "external"))
    assert set(discover_sources(str(main), str(tmp_path / "cwd"))) == {
        main,
        nearby,
        working,
    }


def test_malformed_and_encoded_sources_are_selectable(tmp_path):
    main = write(tmp_path, "main.py", "import broken, encoded\n")
    broken = write(tmp_path, "broken.py", "import import what\n")
    encoded = tmp_path / "encoded.py"
    encoded.write_bytes(b"# coding: latin-1\n# caf\xe9\nimport child\n")
    child = write(tmp_path, "child.py")
    assert set(discover_sources(str(main))) == {main, broken, encoded, child}


def test_additional_roots_are_recursive_and_deduplicated(tmp_path):
    main = write(tmp_path, "main.py")
    extra = write(tmp_path, "external/extra.py", "from . import sibling\n")
    sibling = write(tmp_path, "external/sibling.py")
    assert set(discover_sources(str(main), additional=[extra, extra, main])) == {
        main,
        extra,
        sibling,
    }


def test_non_python_does_not_parse_or_include_binary(tmp_path):
    binary = write(tmp_path, "program", "binary")
    source = write(tmp_path, "source.c", "#include <stdio.h>")
    assert discover_sources(str(binary), additional=[source], python=False) == [source]
    assert discover_sources(str(source), python=False) == [source]


def test_source_list_paths_are_literal_relative_to_list(tmp_path, monkeypatch):
    a = write(tmp_path, "sources/with space.py")
    b = write(tmp_path, "outside.py")
    literal = write(tmp_path, "sources/[literal].py")
    listing = write(
        tmp_path,
        "sources/list.txt",
        f"with space.py\n\n{b}\n./with space.py\nmissing.py\n[literal].py\n",
    )
    monkeypatch.chdir(tmp_path.parent)
    paths, warnings = read_source_list(str(listing))
    assert set(paths) == {a, b, literal}
    assert len(paths) == 3
    assert len(warnings) == 1 and "missing.py" in warnings[0]


def test_source_list_ignores_blank_lines_and_hash_comments(tmp_path):
    source = write(tmp_path, "with space.py")
    listing = write(
        tmp_path,
        "sources.txt",
        "\n \t\n# full-line comment\n  # indented comment\n"
        "with space.py # inline comment # more comment\n"
        "with space.py#comment without a separating space\n",
    )
    paths, warnings = read_source_list(str(listing))
    assert paths == [source]
    assert warnings == []


def test_list_read_errors_are_not_silenced(tmp_path):
    with pytest.raises(OSError):
        read_source_list(str(tmp_path / "missing"))
    bad = tmp_path / "bad"
    bad.write_bytes(b"\xff")
    with pytest.raises(UnicodeError):
        read_source_list(str(bad))


def test_no_program_or_missing_files(tmp_path):
    assert discover_sources("", str(tmp_path)) == []
    assert discover_sources(str(tmp_path / "missing.py")) == []


def test_extensionless_script_is_source_but_native_binary_is_not(tmp_path):
    script = write(tmp_path, "launch", "#!/bin/tcsh\necho hi\n")
    binary = tmp_path / "native"
    binary.write_bytes(b"\x7fELF\x00\x01")
    assert discover_sources(str(script), python=False) == [script]
    assert discover_sources(str(binary), python=False) == []


def test_namespace_directory_does_not_mask_later_package(tmp_path):
    main = write(tmp_path, "script/main.py", "import pkg\n")
    (tmp_path / "script/pkg").mkdir()
    package = write(tmp_path, "cwd/pkg/__init__.py", "import dep\n")
    dep = write(tmp_path, "cwd/dep.py")
    assert set(discover_sources(str(main), str(tmp_path / "cwd"))) == {
        main,
        package,
        dep,
    }


def test_regular_package_blocks_other_roots_for_submodules(tmp_path):
    main = write(tmp_path, "script/main.py", "import pkg.child\n")
    write(tmp_path, "script/pkg/__init__.py")
    write(tmp_path, "cwd/pkg/child.py")
    # pkg.child cannot resolve through a different root once pkg is regular.
    assert tmp_path / "cwd/pkg/child.py" not in discover_sources(
        str(main), str(tmp_path / "cwd")
    )
