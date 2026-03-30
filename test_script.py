"""Simple test script for debugging with tdbg."""


def greet(name):
    message = f"Hello, {name}!"
    print(message)
    return message


def main():
    names = ["Alice", "Bob", "Charlie"]
    for name in names:
        result = greet(name)
        print(f"Result: {result}")
    print("Done!")


if __name__ == "__main__":
    main()
