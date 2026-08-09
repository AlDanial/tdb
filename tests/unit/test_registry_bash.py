"""Detection of bash targets (extension + shebang)."""

import pytest

from tdb.languages import registry


def test_sh_extension_detects_bash(tmp_path):
    p = tmp_path / "deploy.sh"
    p.write_text("echo hi\n")
    assert registry.detect(str(p)) == "bash"


def test_bash_extension_detects_bash(tmp_path):
    p = tmp_path / "deploy.bash"
    p.write_text("echo hi\n")
    assert registry.detect(str(p)) == "bash"


@pytest.mark.parametrize(
    "shebang", ["#!/bin/bash\n", "#!/usr/bin/env bash\n", "#!/usr/local/bin/bash -eu\n"]
)
def test_bash_shebang_detects_bash(tmp_path, shebang):
    p = tmp_path / "deploy"  # no extension
    p.write_text(shebang + "echo hi\n")
    assert registry.detect(str(p)) == "bash"


def test_python_shebang_still_wins(tmp_path):
    p = tmp_path / "tool"
    p.write_text("#!/usr/bin/env python3\n")
    assert registry.detect(str(p)) == "python"
