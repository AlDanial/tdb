let counter = Atomic.make 0

let worker n =
  for _ = 1 to 100_000 do
    Atomic.incr counter;          (* breakpoint line for domain tests *)
    Domain.cpu_relax ()
  done;
  n

let () =
  let ds = List.init 3 (fun i -> Domain.spawn (fun () -> worker (i + 1))) in
  let results = List.map Domain.join ds in
  Printf.printf "sum=%d total=%d\n"
    (List.fold_left ( + ) 0 results)
    (Atomic.get counter)
