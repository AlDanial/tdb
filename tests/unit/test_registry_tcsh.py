"""Detection of tcsh targets (extension + shebang)."""

import pytest

from tdb.languages import registry


def test_csh_extension_detects_tcsh(tmp_path):
    p = tmp_path / "report.csh"
    p.write_text("echo hi\n")
    assert registry.detect(str(p)) == "tcsh"


def test_tcsh_extension_detects_tcsh(tmp_path):
    p = tmp_path / "report.tcsh"
    p.write_text("echo hi\n")
    assert registry.detect(str(p)) == "tcsh"


@pytest.mark.parametrize(
    "shebang", ["#!/bin/tcsh\n", "#!/usr/bin/env tcsh\n", "#!/bin/csh -f\n"]
)
def test_tcsh_shebang_detects_tcsh(tmp_path, shebang):
    p = tmp_path / "report"  # no extension
    p.write_text(shebang + "echo hi\n")
    assert registry.detect(str(p)) == "tcsh"


def test_bash_shebang_still_wins(tmp_path):
    p = tmp_path / "tool"
    p.write_text("#!/usr/bin/env bash\necho hi\n")
    assert registry.detect(str(p)) == "bash"
