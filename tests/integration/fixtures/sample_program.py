"""Sample debuggee for integration tests.

Kept deliberately simple and deterministic so RPC tests can predict
breakpoint hits and variable values.
"""


def add(a, b):
    total = a + b
    return total


def main():
    x = 1
    y = 2
    z = add(x, y)
    print(f"result={z}")
    return z


if __name__ == "__main__":
    main()
