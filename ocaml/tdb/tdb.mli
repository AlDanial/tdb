(** Live breakpoint hook for the tdb debugger, the OCaml counterpart of
    Python's [tdb.breakpoint()].

    Run the program directly, not under tdb. When [breakpoint ()] is
    reached it starts [tdb --lang ocaml -a <pid> --no-pause-on-attach] on
    the program's terminal (the [tdb] on PATH, or the one named by [$TDB]),
    waits for tdb's lldb or gdb to attach, and stops; tdb steps out to the
    call line in your code. Later calls reuse the running tdb. Quitting tdb
    (Ctrl+q) detaches and the program runs on. The call is a no-op when
    stdin or stdout is not a terminal, and it warns on stderr and continues
    when tdb cannot be found, exits early, or does not attach within 60 s.
    Native code only (ocamlopt, dune's dev profile keeps debug info); Linux
    only (ptrace via PR_SET_PTRACER). *)

val breakpoint : unit -> unit
