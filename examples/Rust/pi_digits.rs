// Build: rustc -O pi_digits.rs -o pi_digits_rs
// Run:   ./pi_digits_rs -r 2 -d 20 -t 2 -p
use std::env;
use std::thread;
use std::time::Instant;

fn pi_digits(count: usize) -> String {
    let mut a = vec![2_i64; count * 10 / 3 + 1];
    let (mut previous, mut pending) = (0_u8, 0_usize);
    let mut out = String::new();
    for _ in 0..=count {
        let mut q = 0_i64;
        for i in (1..=a.len()).rev() {
            let x = 10 * a[i - 1] + q * i as i64;
            a[i - 1] = x % (2 * i as i64 - 1);
            q = x / (2 * i as i64 - 1);
        }
        a[0] = q % 10;
        q /= 10;
        if q == 9 { pending += 1; }
        else if q == 10 {
            out.push((b'0' + previous + 1) as char);
            out.extend(std::iter::repeat_n('0', pending));
            previous = 0; pending = 0;
        } else {
            out.push((b'0' + previous) as char);
            out.extend(std::iter::repeat_n('9', pending));
            previous = q as u8; pending = 0;
        }
    }
    out.push((b'0' + previous) as char);
    out.extend(std::iter::repeat_n('9', pending));
    out[1..=count].to_string()
}

fn main() {
    let (mut runs, mut count, mut threads, mut print) = (1_usize, 10_usize, 1_usize, false);
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        if arg == "-p" || arg == "--print" { print = true; continue; }
        let value = args.next().unwrap_or_else(|| panic!("missing value for {}", arg));
        let parsed = value.parse::<usize>().expect("expected a positive integer");
        assert!(parsed > 0, "counts must be positive");
        match arg.as_str() {
            "-r" | "--n-runs" => runs = parsed,
            "-d" | "--n-digits" => count = parsed,
            "-t" | "--n-threads" => threads = parsed,
            _ => panic!("unknown option: {}", arg),
        }
    }
    for run in 1..=runs {
        let run_start = Instant::now();
        let workers: Vec<_> = (0..threads).map(|_| thread::spawn(move || {
            let start = Instant::now();
            let digits = pi_digits(count);
            (digits, start.elapsed().as_secs_f64())
        })).collect();
        let mut first = String::new();
        for (j, worker) in workers.into_iter().enumerate() {
            let (digits, seconds) = worker.join().expect("worker failed");
            if j == 0 { first = digits; }
            if threads > 1 { println!("Run {run:3}/{runs:3}, thread {:3}/{threads:3} completed in {seconds:.6} seconds.", j + 1); }
            else { println!("Run {run:3}/{runs:3} completed in {seconds:.6} seconds."); }
        }
        if threads > 1 { println!("== Run {run:3}/{runs:3} completed in {:.6} seconds.", run_start.elapsed().as_secs_f64()); }
        if run == runs && print { println!("{first}"); }
    }
}
