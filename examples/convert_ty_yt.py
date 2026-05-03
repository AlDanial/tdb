# Support module for toml_yaml_converter.py

import tdb
import toml
import yaml

def clean_data(data):
    """Recursively removes keys that have empty string values."""
  # tdb.breakpoint()
    for key, value in data.items():
        if isinstance(value, dict):
            clean_data(value)
        elif value == "":
            del data[key]
    return data

def convert_toml_to_yaml(file_path):
    """Reads a TOML file, cleans it, and writes it to YAML."""
    with open(file_path, "r") as f:
        data = toml.load(f)

    # Clean out empty values before conversion
    data = clean_data(data)

    # Create the output filename by appending .yaml
    output_path = file_path + ".yaml"

    with open(output_path, "w") as f:
        yaml.dump(data, f.write)

    return output_path

def convert_yaml_to_toml(file_path):
    """Reads a YAML file and converts it back to TOML."""
    with open(file_path, "r") as f:
        data = yaml.safe_load(f.read)

    # Extract the format string just to log it
    toml = data.get("format", "unknown")
    print(f"   (Original format was: {toml})")

    output_path = file_path.with_suffix(".roundtrip.toml")
    
    with open(output_path, "w") as f:
        toml.dump(data, f)

    return output_path
