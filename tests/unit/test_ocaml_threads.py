from tdb.dap.types import StackFrame, Thread
from tdb.languages.ocaml import classify_ocaml_threads


def _frames(*names):
    return [StackFrame(id=i, name=n) for i, n in enumerate(names)]


def test_domains_numbered_backups_hidden():
    threads = [
        Thread(1, "prog"),
        Thread(2, "prog"),
        Thread(3, "prog"),
        Thread(4, "prog"),
    ]
    stacks = {
        1: _frames("camlMain__entry", "caml_start_program", "main"),
        2: _frames("caml_thread_condwait", "backup_thread_func"),
        3: _frames("camlMain__worker_271", "domain_thread_func", "start_thread"),
        4: _frames("caml_thread_condwait", "backup_thread_func"),
    }
    decs = classify_ocaml_threads(threads, stacks)
    assert [d.thread.id for d in decs] == [1, 2, 3, 4]
    assert decs[0].label == "Domain 0 (main)" and not decs[0].hidden
    assert decs[1].hidden
    assert decs[2].label == "Domain 1" and not decs[2].hidden
    assert decs[3].hidden


def test_missing_stack_degrades_to_visible_unlabeled():
    threads = [Thread(1, "prog"), Thread(9, "mystery")]
    decs = classify_ocaml_threads(threads, {})
    assert decs[0].label == "Domain 0 (main)"  # first thread is main
    assert not decs[1].hidden and decs[1].label is None


def test_capability_field_default_none():
    from tdb.languages.base import ProfileCapabilities

    assert ProfileCapabilities().classify_threads is None


def test_native_profile_has_classifier_bytecode_does_not():
    from tdb.languages.ocaml import build_ocaml_profile

    assert build_ocaml_profile(program=None).capabilities.classify_threads is not None
    assert (
        build_ocaml_profile(adapter="ocamlearlybird").capabilities.classify_threads
        is None
    )
