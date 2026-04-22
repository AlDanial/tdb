"""Demo: drop into tdb at a specific line via tdb.breakpoint()."""

import tdb


def compute(n):
    total = 0
    local_list = [1, 2, 3, 4, 5]
    for i in range(n):
        total += i
    tdb.breakpoint()  # tdb should pause here; inspect total and local_list
    return total


if __name__ == "__main__":
    result = compute(10)
    print(f"result = {result}")
