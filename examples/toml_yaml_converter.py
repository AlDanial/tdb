#!/usr/bin/env python3

"""
A program full of bugs to useful to test various tdb features.

Bug 1: Dictionary size changed during iteration
    Location: convert_ty_yt.py, line  'del data[key]'
    Traceback: RuntimeError: dictionary changed size during iteration
    The Error: You cannot delete items from a dictionary while iterating directly over its .items().
    The Fix: Iterate over a list/copy of the keys or items: for key, value in list(data.items()):

Bug 2: Unsupported operand types for +
    Location: convert_ty_yt.py, line  'output_path = file_path + ".yaml"'
    Traceback: TypeError: unsupported operand type(s) for +: 'PosixPath' and 'str' (or WindowsPath)
    The Error: toml_yaml_converter.py passes file_path as a pathlib.Path object, not a string.
    You cannot use the + operator to concatenate a string to a Path object.
    The Fix: Use pathlib's built in suffix modifier: output_path = file_path.with_suffix(".yaml")
    or cast it to a string first: str(file_path) + ".yaml".

Bug 3: Passing a method instead of a stream
    Location: convert_ty_yt.py, line 25  'yaml.dump(data, f.write)'
    Traceback: AttributeError: 'builtin_function_or_method' object has no attribute 'write'
    The Error: yaml.dump expects a file-like object (stream) to write to, but the code passes
    the f.write function itself. The yaml library internally attempts to call .write() on whatever
    is passed to it.
    The Fix: Pass the file object directly: yaml.dump(data, f)

Bug 4: Missing parentheses on method call
    Location: convert_ty_yt.py, line  'data = yaml.safe_load(f.read)'
    Traceback: TypeError: expected string or bytes-like object, got 'builtin_function_or_method'
    The Error: Missing parentheses on f.read(). The code passes the method reference to the
    YAML parser instead of the actual string contents of the file.
    The Fix: Add parentheses: yaml.safe_load(f.read()) (or simply pass f, as yaml.safe_load
    also accepts file streams).

Bug 5: Name shadowing a module
    Location: convert_ty_yt.py, line  'toml.dump(data, f)'
    Traceback: AttributeError: 'str' object has no attribute 'dump'
    The Error: On line 35, a local variable is named toml (toml = data.get("format", "unknown")).
    This shadows the globally imported toml module. By the time the script tries to call
    toml.dump(), toml is the string "toml", not the module!
    The Fix: Rename the local variable on lines 35 and 36 to something else, like original_format.
"""

import tdb
import sys
sys.excepthook = tdb.exception_hook # automatically open tdb on exception
from pathlib import Path
from convert_ty_yt import convert_toml_to_yaml, convert_yaml_to_toml

def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {__file__} <input.toml>")
        sys.exit(1)

    # We use pathlib for modern path handling
    input_file = Path(sys.argv[1])

    if not input_file.exists():
        print(f"Error: The file {input_file} does not exist.")
        sys.exit(1)

    print(f"Step 1: Converting {input_file} to YAML...")
    yaml_file = convert_toml_to_yaml(input_file)
    print(f"Success! Created {yaml_file}")

    print(f"\nStep 2: Converting {yaml_file} back to TOML for verification...")
    roundtrip_file = convert_yaml_to_toml(yaml_file)
    print(f"Success! Created {roundtrip_file}")

if __name__ == "__main__":
    main()
