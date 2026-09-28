// Demo: drop into tdb at a specific line via tdb::breakpoint().
//
// Run it directly (not under tdb). The dev profile keeps locals
// inspectable:
//
//     cd examples/Rust/breakpoint_hook_demo
//     cargo run

fn compute(n: i32) -> i32 {
    let mut total = 0;
    let local_list = vec![1, 2, 3, 4, 5];
    for i in 0..n {
        total += i;
    }
    tdb::breakpoint(); // tdb opens here; inspect total and local_list
    total + local_list.len() as i32
}

fn main() {
    let result = compute(10);
    println!("result = {result}");
}
