(* Demo: drop into tdb at a specific line via Tdb.breakpoint ().

   Run it directly (not under tdb). Without dune, build against the
   library sources in this repository (ocamlopt keeps debug info with -g):

     cp ../../ocaml/tdb/{tdb.ml,tdb.mli,tdb_stubs.c,tdb.h} .
     ocamlopt -g -o demo tdb_stubs.c tdb.mli tdb.ml breakpoint_hook_demo.ml && ./demo

   With dune, add `(libraries tdb)` to your executable stanza and vendor
   or `opam pin` the ocaml/tdb directory. *)

let compute n =
  let total = ref 0 in
  let local_list = [ 1; 2; 3; 4; 5 ] in
  for i = 0 to n - 1 do
    total := !total + i
  done;
  Tdb.breakpoint ();
  (* tdb opens paused on this line; ocamlopt emits no debug info for
     locals, so total and local_list won't appear in Variables (globals
     and the stack are still there) *)
  !total + List.length local_list

let () =
  let result = compute 10 in
  Printf.printf "result = %d\n" result
