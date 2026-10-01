"""The gdb-side STL pretty-printer helper, outside gdb: its search for
gcc's libstdc++ printers and the adapter argv that sources it."""

from __future__ import annotations

import os

from tdb.adapters.native import gdb_stl_printers as helper
from tdb.languages.cpp import STL_PRINTERS_SCRIPT, GdbDapAdapter, gdb_init_args
from tdb.languages.rust import RustGdbAdapter


def test_gdb_init_args_source_the_packaged_script():
    flag, command = gdb_init_args()
    assert flag == "-iex"
    assert command.startswith("source ")
    path = command[len("source ") :]
    assert os.path.basename(path) == STL_PRINTERS_SCRIPT
    assert os.path.isfile(path)


def test_gdb_adapter_sources_script_before_dap_mode():
    argv = GdbDapAdapter(executable="/opt/gdb").command()
    assert argv[:2] == ["/opt/gdb", "-iex"]
    assert argv[2].endswith(STL_PRINTERS_SCRIPT)
    assert argv[-2:] == ["-i", "dap"]


def test_rust_gdb_adapter_keeps_stl_script():
    argv = RustGdbAdapter(executable="/opt/gdb").command()
    assert any(a.endswith(STL_PRINTERS_SCRIPT) for a in argv)
    assert argv[-2:] == ["-i", "dap"]


def test_importable_without_gdb_registers_nothing():
    assert helper.gdb is None


def test_strip_std_template():
    f = helper.strip_std_template
    assert f("std::vector<int, std::allocator<int> >") == "vector"
    assert (
        f(
            "std::__cxx11::basic_string<char, std::char_traits<char>, std::allocator<char> >"
        )
        == "basic_string"
    )
    assert f("std::__debug::map<int, int>") == "map"
    assert f("std::_Rb_tree_node<int>") == "_Rb_tree_node"
    assert f("std::__detail::_Hash_node<int, false>") is None  # nested namespace
    assert f("std::string") is None  # not a template specialization
    assert f("Result") is None
    assert f("boost::vector<int>") is None


def _printers_tree(root, name="gcc"):
    """A `<root>/share/<name>/python/libstdcxx/v6/printers.py` tree; returns
    the python directory."""
    pythondir = root / "share" / name / "python"
    (pythondir / "libstdcxx" / "v6").mkdir(parents=True)
    (pythondir / "libstdcxx" / "v6" / "printers.py").write_text("# stub\n")
    return pythondir


def test_find_libstdcxx_dir_wants_the_printers_module(tmp_path):
    good = _printers_tree(tmp_path / "good")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert helper.find_libstdcxx_dir([str(empty), str(good)]) == str(good)
    assert helper.find_libstdcxx_dir([str(empty)]) is None


def test_explicit_directory_is_the_only_candidate(tmp_path):
    dirs = helper.candidate_dirs(
        str(tmp_path), ["/usr/lib/x86_64-linux-gnu/libstdc++.so.6"], "/usr/share/gdb"
    )
    assert dirs == [str(tmp_path)]


def test_mode_words_are_not_directories(tmp_path):
    for word in ("auto", "AUTO", "bundled", "off", "", None):
        assert helper.candidate_dirs(word, [], None, fixed_globs=()) == []


def test_auto_load_script_names_the_python_directory(tmp_path):
    # A distro layout: gdb's auto-load tree holds <libstdc++ path>-gdb.py
    # whose `pythondir = '...'` line points at gcc's python directory.
    lib = tmp_path / "usr" / "lib" / "x86_64-linux-gnu" / "libstdc++.so.6.0.35"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"")
    datadir = tmp_path / "usr" / "share" / "gdb"
    script = datadir / "auto-load" / str(lib).lstrip("/")
    script = script.with_name(script.name + "-gdb.py")
    script.parent.mkdir(parents=True)
    script.write_text("import gdb\npythondir = '/opt/gcc/python'\nlibdir = '/x'\n")
    dirs = helper.candidate_dirs(
        None, [str(lib)], None, auto_load_data_dirs=(str(datadir),), fixed_globs=()
    )
    assert dirs[0] == "/opt/gcc/python"


def test_auto_load_script_also_tried_under_gdbs_own_data_directory(tmp_path):
    lib = tmp_path / "lib" / "libstdc++.so.6"
    lib.parent.mkdir()
    lib.write_bytes(b"")
    datadir = tmp_path / "gdb-data"
    script = datadir / "auto-load" / str(lib).lstrip("/")
    script = script.with_name(script.name + "-gdb.py")
    script.parent.mkdir(parents=True)
    script.write_text('pythondir = "/from/datadir"\n')
    dirs = helper.candidate_dirs(
        None, [str(lib)], str(datadir), auto_load_data_dirs=(), fixed_globs=()
    )
    assert "/from/datadir" in dirs


def test_candidates_near_the_libstdcxx_objfile(tmp_path):
    # <prefix>/lib64/libstdc++.so.6 -> <prefix>/share/gcc-15/python, and
    # <prefix>/lib/gcc/x86_64/15/libstdc++.so -> <prefix>/share/gcc/python
    # (one and three levels up).
    prefix = tmp_path / "prefix"
    one = prefix / "lib64" / "libstdc++.so.6"
    one.parent.mkdir(parents=True)
    one.write_bytes(b"")
    three = prefix / "lib" / "gcc" / "x86_64" / "libstdc++.so"
    three.parent.mkdir(parents=True)
    three.write_bytes(b"")
    pythondir = _printers_tree(prefix, "gcc-15")
    dirs = helper.candidate_dirs(
        None,
        ["/bin/true", str(one), str(three)],
        None,
        auto_load_data_dirs=(),
        fixed_globs=(),
    )
    assert dirs == [str(pythondir)]  # found twice, listed once; /bin/true ignored


def test_candidates_beside_gdbs_data_directory_and_fixed_globs(tmp_path):
    beside = _printers_tree(tmp_path / "share-root")  # <root>/share/gcc/python
    datadir = tmp_path / "share-root" / "share" / "gdb"
    datadir.mkdir()
    fixed = _printers_tree(tmp_path / "fixed", "gcc-14")
    dirs = helper.candidate_dirs(
        None,
        [],
        str(datadir),
        auto_load_data_dirs=(),
        fixed_globs=(str(tmp_path / "fixed" / "share" / "gcc*" / "python"),),
    )
    assert dirs == [str(beside), str(fixed)]


def test_pythondir_from_missing_or_plain_script(tmp_path):
    assert helper.pythondir_from_auto_load_script(str(tmp_path / "nope")) is None
    plain = tmp_path / "plain-gdb.py"
    plain.write_text("import gdb\n")
    assert helper.pythondir_from_auto_load_script(str(plain)) is None


def test_is_libstdcxx_objfile():
    assert helper.is_libstdcxx_objfile("/usr/lib/x86_64-linux-gnu/libstdc++.so.6.0.35")
    assert helper.is_libstdcxx_objfile("/mingw64/bin/libstdc++-6.dll")
    assert helper.is_libstdcxx_objfile("/opt/homebrew/lib/gcc/15/libstdc++.6.dylib")
    assert not helper.is_libstdcxx_objfile("/usr/lib/x86_64-linux-gnu/libc.so.6")
    assert not helper.is_libstdcxx_objfile("/tmp/prog")
