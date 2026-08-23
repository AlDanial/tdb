from tdb.languages.ocaml import demangle_frame_name


def test_simple_symbol():
    assert demangle_frame_name("camlMain__worker_271") == "Main.worker"


def test_nested_modules():
    assert demangle_frame_name("camlFoo__Bar__run_17") == "Foo.Bar.run"


def test_no_numeric_suffix():
    assert demangle_frame_name("camlMain__entry") == "Main.entry"


def test_runtime_c_symbols_untouched():
    for name in (
        "caml_apply2",
        "caml_start_program",
        "main",
        "pthread_cond_wait",
        "camlcase_but_no_sep",
    ):
        assert demangle_frame_name(name) == name


def test_dotted_module_path():
    assert demangle_frame_name("camlOcaml_domains.worker_297") == "Ocaml_domains.worker"


def test_dotted_nested_module_path():
    assert demangle_frame_name("camlStdlib__Domain.body_757") == "Stdlib.Domain.body"


def test_presentation_has_frame_name_field():
    from tdb.languages.base import Presentation

    assert Presentation().frame_name is None
