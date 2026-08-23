let boom () = failwith "boom"
let middle () = boom ()
let () = middle ()
