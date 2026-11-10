import sys

import pytest

from tdb.source_discovery import discover_sources, read_source_list


def write(root, name, content=""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_resolves_static_requests_without_importing_or_recursing(tmp_path, monkeypatch):
    from tdb.source_search import search_module_files

    project = tmp_path / "project"
    external = tmp_path / "external"
    main = write(project, "main.py", "import pkg.child\nimport missing\n")
    marker = tmp_path / "executed"
    init = write(external, "pkg/__init__.py", f"open({str(marker)!r}, 'w').close()")
    child = write(external, "pkg/child.py", "import unrelated\n")
    write(external, "unrelated.py")
    monkeypatch.setenv("PYTHONPATH", str(external) + ":" + str(tmp_path / "absent"))
    requests = []
    assert discover_sources(str(main), str(project), requests=requests) == [main]
    assert set(
        search_module_files(requests, python=sys.executable, cwd=str(project))
    ) == {init, child}
    assert not marker.exists()


def test_selected_interpreter_and_stdlib_filter(tmp_path):
    import subprocess

    from tdb.source_search import search_module_files

    # A real venv proves the query uses the chosen interpreter's site-packages.
    venv = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True
    )
    python = venv / "bin/python"
    site = next((venv / "lib").glob("python*/site-packages"))
    dependency = write(site, "only_in_selected_env.py")
    main = write(tmp_path, "main.py", "import only_in_selected_env\nimport fractions\n")
    requests = []
    discover_sources(str(main), requests=requests)
    assert search_module_files(requests, python=str(python)) == [dependency]
    all_files = search_module_files(requests, python=str(python), just_my_code=False)
    assert dependency in all_files
    assert any(p.name == "fractions.py" for p in all_files)


def test_metadata_imports_do_not_execute_project_shadows(tmp_path, monkeypatch):
    from tdb.source_search import search_module_files

    marker = tmp_path / "executed"
    for name in ("json", "sysconfig"):
        write(
            tmp_path,
            name + ".py",
            f"open({str(marker)!r}, 'w').close()\nraise RuntimeError('shadow')",
        )
    module = write(tmp_path, "dependency.py")
    main = write(tmp_path, "main.py", "import dependency")
    requests = []
    discover_sources(str(main), requests=requests)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert module in search_module_files(requests, cwd=str(tmp_path))
    assert not marker.exists()


def test_dynamic_search_preserves_local_package_boundary(tmp_path, monkeypatch):
    from tdb.source_search import search_module_files

    main = write(tmp_path, "project/main.py", "import pkg.child")
    init = write(tmp_path, "project/pkg/__init__.py")
    write(tmp_path, "external/pkg/child.py")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "external"))
    requests = []
    discover_sources(str(main), str(main.parent), requests=requests)
    assert search_module_files(requests) == [init]


def test_write_list_roundtrip_and_failed_replace_keeps_original(tmp_path, monkeypatch):
    from tdb.source_search import write_source_list

    source = write(tmp_path, "with spaces.py")
    listing = tmp_path / "sources.txt"
    write_source_list(listing, [source])
    assert read_source_list(str(listing)) == ([source], [])
    before = listing.read_bytes()

    def fail(*args):
        raise OSError("replacement denied")

    monkeypatch.setattr("tdb.source_search.os.replace", fail)
    with pytest.raises(OSError, match="replacement denied"):
        write_source_list(listing, [])
    assert listing.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "sources.txt",
        "with spaces.py",
    ]


def test_save_rejects_unrepresentable_names_without_replacing(tmp_path):
    from tdb.source_search import write_source_list

    listing = write(tmp_path, "sources.txt", "original\n")
    with pytest.raises(ValueError):
        write_source_list(listing, [tmp_path / "hash#name.py"])
    assert listing.read_text() == "original\n"


@pytest.mark.parametrize(
    "name", ["line\nbreak.py", "unicode\u2028break.py", "trailing.py "]
)
def test_save_rejects_paths_that_cannot_roundtrip(tmp_path, name):
    from tdb.source_search import write_source_list

    listing = write(tmp_path, "sources.txt", "original\n")
    with pytest.raises(ValueError):
        write_source_list(listing, [tmp_path / name])
    assert listing.read_text() == "original\n"


def test_stdlib_filter_preserves_nested_site_packages(tmp_path, monkeypatch):
    import json
    import subprocess

    from tdb.source_search import search_module_files

    stdlib = tmp_path / "stdlib"
    site = stdlib / "site-packages"
    standard = write(stdlib, "standard.py")
    third_party = write(site, "third_party.py")
    main = write(tmp_path, "project/main.py", "import standard, third_party")
    requests = []
    discover_sources(str(main), str(main.parent), requests=requests)
    payload = {
        "paths": [str(stdlib), str(site)],
        "stdlib": [str(stdlib)],
        "site": [str(site)],
    }
    monkeypatch.setattr(
        "tdb.source_search.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, "TDB_SOURCE_PATHS:" + json.dumps(payload), ""
        ),
    )
    assert search_module_files(requests) == [third_party]
    assert set(search_module_files(requests, just_my_code=False)) == {
        standard,
        third_party,
    }


def test_relative_requests_and_namespace_search(tmp_path, monkeypatch):
    from tdb.source_search import search_module_files

    main = write(tmp_path, "project/main.py", "import pkg.local, ns.remote")
    write(tmp_path, "project/pkg/__init__.py")
    local = write(tmp_path, "project/pkg/local.py", "from . import late")
    requests = []
    discover_sources(str(main), str(main.parent), requests=requests)
    late = write(tmp_path, "project/pkg/late.py")
    remote = write(tmp_path, "external/ns/remote.py")
    write(tmp_path, "project/ns/unrelated.py")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "external"))
    results = search_module_files(requests)
    assert {local, late, remote}.issubset(results)
    assert all(path.name != "unrelated.py" for path in results)
