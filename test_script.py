"""Simple test script for debugging with tdbg."""
import test_module


def greet(name):
    message = f"Hello, {name}!"
    print(message)
    return message


def main():
    names = ["Alice", "Bob", "Charlie"]
    test_module.a_function()
    for name in names:
        result = greet(name)
        print(f"Result: {result}")
    print("Done!")


if __name__ == "__main__":
    main()
