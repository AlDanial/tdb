//! Deterministic blocking scenarios for real Rust debugger integrations.

use std::hint::black_box;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process;
use std::sync::{mpsc, Arc, Barrier, Condvar, Mutex, RwLock};
use std::thread;

struct FixtureArgs {
    scenario: String,
    ready_port: Option<u16>,
    control: bool,
}

fn park_forever() -> ! {
    loop {
        thread::park();
    }
}

fn announce_ready(args: &FixtureArgs) {
    if let Some(port) = args.ready_port {
        let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        stream.write_all(args.scenario.as_bytes()).unwrap();
        stream.flush().unwrap();
        let mut proof = [0_u8; 1];
        stream.read_exact(&mut proof).unwrap();
        assert_eq!(proof[0], b'A', "debugger wait proof was not acknowledged");
        println!("READY:{}", args.scenario);
        std::io::stdout().flush().unwrap();
        stream.write_all(b"R").unwrap();
        stream.flush().unwrap();
        if args.control {
            thread::Builder::new()
                .name("fixture-control".into())
                .spawn(move || {
                    let mut command = [0_u8; 1];
                    if stream.read_exact(&mut command).is_ok() && command[0] == b'X' {
                        process::exit(0);
                    }
                })
                .unwrap();
        }
    } else {
        println!("READY:{}", args.scenario);
        std::io::stdout().flush().unwrap();
    }
}

fn wait_for_workers(ready: mpsc::Receiver<()>, count: usize) {
    for _ in 0..count {
        ready.recv().unwrap();
    }
}

fn join_wait(args: &FixtureArgs) -> ! {
    let barrier = Arc::new(Barrier::new(3));
    let (ready_tx, ready_rx) = mpsc::channel();
    let target_barrier = Arc::clone(&barrier);
    let target_ready = ready_tx.clone();
    let target = thread::Builder::new()
        .name("join-target".into())
        .spawn(move || {
            target_barrier.wait();
            target_ready.send(()).unwrap();
            park_forever();
        })
        .unwrap();
    let join_barrier = Arc::clone(&barrier);
    thread::Builder::new()
        .name("join-waiter".into())
        .spawn(move || {
            join_barrier.wait();
            ready_tx.send(()).unwrap();
            target.join().unwrap();
        })
        .unwrap();
    barrier.wait();
    wait_for_workers(ready_rx, 2);
    announce_ready(args);
    park_forever();
}

fn mutex_wait(args: &FixtureArgs) -> ! {
    let mutex = Arc::new(Mutex::new(()));
    let barrier = Arc::new(Barrier::new(3));
    let (ready_tx, ready_rx) = mpsc::channel();
    let owner_mutex = Arc::clone(&mutex);
    let owner_barrier = Arc::clone(&barrier);
    let owner_ready = ready_tx.clone();
    thread::Builder::new()
        .name("mutex-owner".into())
        .spawn(move || {
            let guard = owner_mutex.lock().unwrap();
            owner_barrier.wait();
            owner_ready.send(()).unwrap();
            black_box(&guard);
            park_forever();
        })
        .unwrap();
    let waiter_barrier = Arc::clone(&barrier);
    thread::Builder::new()
        .name("mutex-waiter".into())
        .spawn(move || {
            waiter_barrier.wait();
            ready_tx.send(()).unwrap();
            let guard = mutex.lock().unwrap();
            black_box(&guard);
            park_forever();
        })
        .unwrap();
    barrier.wait();
    wait_for_workers(ready_rx, 2);
    announce_ready(args);
    park_forever();
}

fn rwlock_read_wait(args: &FixtureArgs) -> ! {
    let lock = Arc::new(RwLock::new(()));
    let barrier = Arc::new(Barrier::new(3));
    let (ready_tx, ready_rx) = mpsc::channel();
    let owner_lock = Arc::clone(&lock);
    let owner_barrier = Arc::clone(&barrier);
    let owner_ready = ready_tx.clone();
    thread::Builder::new()
        .name("rwlock-writer".into())
        .spawn(move || {
            let guard = owner_lock.write().unwrap();
            owner_barrier.wait();
            owner_ready.send(()).unwrap();
            black_box(&guard);
            park_forever();
        })
        .unwrap();
    let waiter_barrier = Arc::clone(&barrier);
    thread::Builder::new()
        .name("rwlock-reader".into())
        .spawn(move || {
            waiter_barrier.wait();
            ready_tx.send(()).unwrap();
            let guard = lock.read().unwrap();
            black_box(&guard);
            park_forever();
        })
        .unwrap();
    barrier.wait();
    wait_for_workers(ready_rx, 2);
    announce_ready(args);
    park_forever();
}

fn rwlock_write_wait(args: &FixtureArgs) -> ! {
    let lock = Arc::new(RwLock::new(()));
    let barrier = Arc::new(Barrier::new(3));
    let (ready_tx, ready_rx) = mpsc::channel();
    let owner_lock = Arc::clone(&lock);
    let owner_barrier = Arc::clone(&barrier);
    let owner_ready = ready_tx.clone();
    thread::Builder::new()
        .name("rwlock-reader".into())
        .spawn(move || {
            let guard = owner_lock.read().unwrap();
            owner_barrier.wait();
            owner_ready.send(()).unwrap();
            black_box(&guard);
            park_forever();
        })
        .unwrap();
    let waiter_barrier = Arc::clone(&barrier);
    thread::Builder::new()
        .name("rwlock-writer".into())
        .spawn(move || {
            waiter_barrier.wait();
            ready_tx.send(()).unwrap();
            let guard = lock.write().unwrap();
            black_box(&guard);
            park_forever();
        })
        .unwrap();
    barrier.wait();
    wait_for_workers(ready_rx, 2);
    announce_ready(args);
    park_forever();
}

fn condvar_wait(args: &FixtureArgs) -> ! {
    let pair = Arc::new((Mutex::new(()), Condvar::new()));
    let barrier = Arc::new(Barrier::new(2));
    let (ready_tx, ready_rx) = mpsc::channel();
    let worker_pair = Arc::clone(&pair);
    let worker_barrier = Arc::clone(&barrier);
    thread::Builder::new()
        .name("condvar-waiter".into())
        .spawn(move || {
            let guard = worker_pair.0.lock().unwrap();
            worker_barrier.wait();
            ready_tx.send(()).unwrap();
            let guard = worker_pair.1.wait(guard).unwrap();
            black_box(&guard);
            park_forever();
        })
        .unwrap();
    barrier.wait();
    wait_for_workers(ready_rx, 1);
    announce_ready(args);
    black_box(&pair);
    park_forever();
}

fn mpsc_send_wait(args: &FixtureArgs) -> ! {
    let (sender, receiver) = mpsc::sync_channel::<u8>(0);
    let barrier = Arc::new(Barrier::new(2));
    let (ready_tx, ready_rx) = mpsc::channel();
    let worker_barrier = Arc::clone(&barrier);
    thread::Builder::new()
        .name("mpsc-sender".into())
        .spawn(move || {
            worker_barrier.wait();
            ready_tx.send(()).unwrap();
            sender.send(1).unwrap();
            park_forever();
        })
        .unwrap();
    barrier.wait();
    wait_for_workers(ready_rx, 1);
    announce_ready(args);
    black_box(&receiver);
    park_forever();
}

fn mpsc_recv_wait(args: &FixtureArgs) -> ! {
    let (sender, receiver) = mpsc::channel::<u8>();
    let barrier = Arc::new(Barrier::new(2));
    let (ready_tx, ready_rx) = mpsc::channel();
    let worker_barrier = Arc::clone(&barrier);
    thread::Builder::new()
        .name("mpsc-receiver".into())
        .spawn(move || {
            worker_barrier.wait();
            ready_tx.send(()).unwrap();
            black_box(receiver.recv().unwrap());
            park_forever();
        })
        .unwrap();
    barrier.wait();
    wait_for_workers(ready_rx, 1);
    announce_ready(args);
    black_box(&sender);
    park_forever();
}

fn park_wait(args: &FixtureArgs) -> ! {
    let barrier = Arc::new(Barrier::new(2));
    let (ready_tx, ready_rx) = mpsc::channel();
    let worker_barrier = Arc::clone(&barrier);
    thread::Builder::new()
        .name("park-waiter".into())
        .spawn(move || {
            worker_barrier.wait();
            ready_tx.send(()).unwrap();
            park_forever();
        })
        .unwrap();
    barrier.wait();
    wait_for_workers(ready_rx, 1);
    announce_ready(args);
    park_forever();
}

fn cycle_wait(args: &FixtureArgs) -> ! {
    let first = Arc::new(Mutex::new(()));
    let second = Arc::new(Mutex::new(()));
    let barrier = Arc::new(Barrier::new(3));
    let (ready_tx, ready_rx) = mpsc::channel();

    let t1_first = Arc::clone(&first);
    let t1_second = Arc::clone(&second);
    let t1_barrier = Arc::clone(&barrier);
    let t1_ready = ready_tx.clone();
    thread::Builder::new()
        .name("cycle-first".into())
        .spawn(move || {
            let first_guard = t1_first.lock().unwrap();
            t1_barrier.wait();
            t1_ready.send(()).unwrap();
            let second_guard = t1_second.lock().unwrap();
            black_box((&first_guard, &second_guard));
            park_forever();
        })
        .unwrap();

    let t2_barrier = Arc::clone(&barrier);
    thread::Builder::new()
        .name("cycle-second".into())
        .spawn(move || {
            let second_guard = second.lock().unwrap();
            t2_barrier.wait();
            ready_tx.send(()).unwrap();
            let first_guard = first.lock().unwrap();
            black_box((&first_guard, &second_guard));
            park_forever();
        })
        .unwrap();

    barrier.wait();
    wait_for_workers(ready_rx, 2);
    announce_ready(args);
    park_forever();
}

fn incomplete_cycle_wait(args: &FixtureArgs) -> ! {
    let mutex = Arc::new(Mutex::new(()));
    let barrier = Arc::new(Barrier::new(3));
    let (ready_tx, ready_rx) = mpsc::channel();
    let owner_mutex = Arc::clone(&mutex);
    let owner_barrier = Arc::clone(&barrier);
    let owner_ready = ready_tx.clone();
    thread::Builder::new()
        .name("incomplete-owner".into())
        .spawn(move || {
            let guard = owner_mutex.lock().unwrap();
            owner_barrier.wait();
            owner_ready.send(()).unwrap();
            black_box(&guard);
            park_forever();
        })
        .unwrap();
    let waiter_barrier = Arc::clone(&barrier);
    thread::Builder::new()
        .name("incomplete-waiter".into())
        .spawn(move || {
            waiter_barrier.wait();
            ready_tx.send(()).unwrap();
            let guard = mutex.lock().unwrap();
            black_box(&guard);
            park_forever();
        })
        .unwrap();
    barrier.wait();
    wait_for_workers(ready_rx, 2);
    announce_ready(args);
    park_forever();
}

fn healthy_blocked(args: &FixtureArgs) -> ! {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let barrier = Arc::new(Barrier::new(2));
    let (ready_tx, ready_rx) = mpsc::channel();
    let worker_barrier = Arc::clone(&barrier);
    thread::Builder::new()
        .name("healthy-parked-worker".into())
        .spawn(move || {
            worker_barrier.wait();
            ready_tx.send(()).unwrap();
            park_forever();
        })
        .unwrap();
    barrier.wait();
    wait_for_workers(ready_rx, 1);
    announce_ready(args);
    black_box(listener.accept().unwrap());
    park_forever();
}

fn main() {
    let mut values = std::env::args().skip(1);
    let args = FixtureArgs {
        scenario: values.next().expect("scenario argument is required"),
        ready_port: values.next().map(|value| value.parse().unwrap()),
        control: matches!(values.next().as_deref(), Some("control")),
    };
    match args.scenario.as_str() {
        "join" => join_wait(&args),
        "mutex" => mutex_wait(&args),
        "rwlock-read" => rwlock_read_wait(&args),
        "rwlock-write" => rwlock_write_wait(&args),
        "condvar" => condvar_wait(&args),
        "mpsc-send" => mpsc_send_wait(&args),
        "mpsc-recv" => mpsc_recv_wait(&args),
        "park" => park_wait(&args),
        "cycle" => cycle_wait(&args),
        "incomplete-cycle" => incomplete_cycle_wait(&args),
        "healthy-blocked" => healthy_blocked(&args),
        _ => panic!("unknown Rust concurrency scenario: {}", args.scenario),
    }
}
