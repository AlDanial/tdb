(* Build: ocamlopt -O2 -I +unix unix.cmxa pi_digits.ml -o pi_digits_ocaml
   Run:   ./pi_digits_ocaml -r 2 -d 20 -t 2 -p
   Requires OCaml 5 for Domain.spawn. *)

let pi_digits count =
  let a = Array.make (count * 10 / 3 + 1) 2 in
  let out = Buffer.create (count + 2) in
  let previous = ref 0 and pending = ref 0 in
  for _ = 0 to count do
    let q = ref 0 in
    for i = Array.length a downto 1 do
      let x = 10 * a.(i - 1) + !q * i in
      a.(i - 1) <- x mod (2 * i - 1);
      q := x / (2 * i - 1)
    done;
    a.(0) <- !q mod 10;
    q := !q / 10;
    if !q = 9 then incr pending
    else if !q = 10 then (
      Buffer.add_char out (Char.chr (Char.code '0' + !previous + 1));
      for _ = 1 to !pending do Buffer.add_char out '0' done;
      previous := 0; pending := 0
    ) else (
      Buffer.add_char out (Char.chr (Char.code '0' + !previous));
      for _ = 1 to !pending do Buffer.add_char out '9' done;
      previous := !q; pending := 0
    )
  done;
  Buffer.add_char out (Char.chr (Char.code '0' + !previous));
  for _ = 1 to !pending do Buffer.add_char out '9' done;
  String.sub (Buffer.contents out) 1 count

let () =
  let runs = ref 1 and count = ref 10 and threads = ref 1 and print = ref false in
  let options = [
    "-r", Arg.Set_int runs, "number of runs";
    "--n-runs", Arg.Set_int runs, "number of runs";
    "-d", Arg.Set_int count, "number of digits";
    "--n-digits", Arg.Set_int count, "number of digits";
    "-t", Arg.Set_int threads, "workers per run";
    "--n-threads", Arg.Set_int threads, "workers per run";
    "-p", Arg.Set print, "print digits";
    "--print", Arg.Set print, "print digits";
  ] in
  Arg.parse options (fun arg -> raise (Arg.Bad ("unexpected argument: " ^ arg))) "pi_digits";
  if !runs < 1 || !count < 1 || !threads < 1 then invalid_arg "counts must be positive";
  for run = 1 to !runs do
    let run_start = Unix.gettimeofday () in
    let workers = Array.init !threads (fun _ -> Domain.spawn (fun () ->
      let start = Unix.gettimeofday () in
      let digits = pi_digits !count in
      digits, Unix.gettimeofday () -. start
    )) in
    let results = Array.map Domain.join workers in
    Array.iteri (fun j (_, elapsed) ->
      if !threads > 1 then
        Printf.printf "Run %3d/%3d, thread %3d/%3d completed in %.6f seconds.\n" run !runs (j + 1) !threads elapsed
      else Printf.printf "Run %3d/%3d completed in %.6f seconds.\n" run !runs elapsed
    ) results;
    if !threads > 1 then Printf.printf "== Run %3d/%3d completed in %.6f seconds.\n" run !runs (Unix.gettimeofday () -. run_start);
    if run = !runs && !print then print_endline (fst results.(0))
  done
