#!/usr/bin/env python3
"""
A copy of digits_of_pi.py with a deliberate error to demonstrate
post-mortem tdb attachment.
"""

import sys
from collections import Counter

# - - - - - \/ - - - - - \/ - - - - - \/ - - - - - \/ - - - - - \/ - - - - - \/
import tdb  # These two lines cause the program to open

sys.excepthook = tdb.exception_hook  # tdb when an unhandled exception occurs.
# - - - - - /\ - - - - - /\ - - - - - /\ - - - - - /\ - - - - - /\ - - - - - /\


def generate_pi_digits():
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


def read_source_file(file_path):
    """Step 3: Reads the file this code is stored in."""
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            return file.read()
    except FileNotFoundError:
        print(f"Error: The file {file_path} was not found.")
        sys.exit(1)


def process_characters(source_code, pi_gen):
    """
    Step 4: Processes the file character by character.
    Updates the frequency table and resumes pi computation when 'i' is encountered.
    """
    letter_freq = Counter()
    computed_pi_digits = []

    for char in source_code:
        if char == "q":
            print(
                'Encountered "q".  Access a non-existent key in a dict to trigger traceback.'
            )
            this_is_a_deliberate["error"] = True
        if char.isalpha():
            letter_freq[char.lower()] += 1

        if char.lower() == "i":
            digit = next(
                pi_gen
            )  # resume pi computation until exactly one new digit is returned
            computed_pi_digits.append(str(digit))

    return letter_freq, computed_pi_digits


def display_pi_results(computed_pi_digits):
    """Step 5: Displays the Pi digits calculated incrementally."""
    print("=== pi Calculation Results ===")
    if computed_pi_digits:
        # Format the digits nicely (e.g. "3.14159...")
        pi_string = computed_pi_digits[0] + "." + "".join(computed_pi_digits[1:])
        print(f"Letter 'i' was encountered {len(computed_pi_digits)} times.")
        print(f"Calculated pi string: {pi_string}\n")
    else:
        print("No 'i's were found in the file, so pi was not calculated.\n")


def display_frequency_table(letter_freq):
    """Step 6: Displays the sorted letter frequency table."""
    print("=== Letter Frequency Table ===")
    print(f"{'Letter':<8} | {'Frequency':<9}")
    print("-" * 20)

    # Sort by frequency (highest first), then alphabetically for ties
    sorted_frequencies = sorted(
        letter_freq.items(), key=lambda item: (-item[1], item[0])
    )

    for letter, count in sorted_frequencies:
        print(f"   {letter:<5} | {count:<9}")


def main():
    pi_gen = generate_pi_digits()  # initialize the pi generator

    try:
        current_file_path = __file__
    except NameError:
        print("Error: __file__ is not defined. Please run this as a script.")
        sys.exit(1)

    source_code = read_source_file(current_file_path)
    letter_freq, computed_pi_digits = process_characters(source_code, pi_gen)
    display_pi_results(computed_pi_digits)
    display_frequency_table(letter_freq)


if __name__ == "__main__":
    main()
