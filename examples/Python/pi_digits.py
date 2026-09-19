#!/usr/bin/env python3
"""
Computes P number of digits of pi N number of times.
"""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor


def parse_args():  # {{{1
    parser = argparse.ArgumentParser(description="Compute digits if pi.")

    parser.add_argument(
        "-r",
        "--n-runs",
        dest="n_runs",
        action="store",
        type=int,
        default=1,
        help="Number of runs to perform [1].",
    )

    parser.add_argument(
        "-d",
        "--n-digits",
        dest="n_digits",
        action="store",
        type=int,
        default=10,
        help="Number of digits to compute [10].",
    )

    parser.add_argument(
        "-t",
        "--n-threads",
        dest="n_threads",
        action="store",
        type=int,
        default=1,
        help="Number of threads per run [1].",
    )

    parser.add_argument(
        "-p",
        "--print",
        dest="print",
        action="store_true",
        default=False,
        help="Print the digits after the last run.",
    )

    args = parser.parse_args()

    return args


# 1}}}
def generate_pi_digits():  # {{{1
    """
    An unbounded generator that calculates digits of pi using the Gibbons spigot algorithm.
    Every time this function yields, it halts computation and hands control
    back to the caller until the next digit is requested.
    """
    q, r, t, k, n, l = 1, 0, 1, 1, 3, 3
    while True:
        if 4 * q + r - t < n * t:
            yield n
            nr = 10 * (r - n * t)
            n = ((10 * (3 * q + r)) // t) - 10 * n
            q *= 10
            r = nr
        else:
            nr = (2 * q + r) * l
            nn = (q * (7 * k + 2) + r * l) // (t * l)
            q *= k
            t *= l
            l += 2
            k += 1
            n = nn
            r = nr


# 1}}}
def get_digits(pi_gen, n: int) -> list[int]:  # {{{1
    return [next(pi_gen) for _ in range(n)]


# 1}}}
def digits_instance(pi_generator, n_digits):  # {{{1
    start_time = time.time()
    digits = get_digits(pi_generator, n_digits)
    end_time = time.time()
    return start_time, digits, end_time


# 1}}}
def main():
    args = parse_args()

    for i in range(args.n_runs):
        # pi_gen, digits, start_time, end_time are per-thread variables
        pi_gen = [None] * args.n_threads
        digits = [None] * args.n_threads
        start_time = [None] * args.n_threads
        end_time = [None] * args.n_threads
        run_start_time = time.time()
        with ThreadPoolExecutor(max_workers=args.n_threads) as executor:
            futures = []
            for j in range(args.n_threads):
                pi_gen[j] = generate_pi_digits()
                futures.append(
                    executor.submit(digits_instance, pi_gen[j], args.n_digits)
                )

            for j, future in enumerate(futures):
                start_time[j], digits[j], end_time[j] = future.result()
                if args.n_threads > 1:
                    print(
                        f"Run {i + 1:3d}/{args.n_runs:3d}, thread {j + 1:3d}/{args.n_threads:3d} completed in {end_time[j] - start_time[j]:.6f} seconds."
                    )
                else:
                    print(
                        f"Run {i + 1:3d}/{args.n_runs:3d} completed in {end_time[j] - start_time[j]:.6f} seconds."
                    )
        run_end_time = time.time()
        if args.n_threads > 1:
            print(
                f"== Run {i + 1:3d}/{args.n_runs:3d} completed in {run_end_time - run_start_time:.6f} seconds."
            )

    if args.print:
        print("".join(str(digit) for digit in digits[0]))


if __name__ == "__main__":
    main()
