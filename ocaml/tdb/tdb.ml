external tdb_ocaml_breakpoint : unit -> unit = "tdb_ocaml_breakpoint"

(* Not inlined: tdb recognizes the hook by its frames (camlTdb.breakpoint_*,
   caml_c_call, tdb_ocaml_breakpoint, tdb_breakpoint_lang,
   tdb_breakpoint_stop) and steps out of them to the caller. *)
let[@inline never] breakpoint () = tdb_ocaml_breakpoint ()
