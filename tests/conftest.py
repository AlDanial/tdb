"""Shared test fixtures.

Adds the project's src/ to sys.path so `import tdb` resolves to the working
copy without requiring an editable install. Tests can also rely on
`pytest-asyncio` being in auto mode (configured in pyproject.toml).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))


# --- tcsh adapter fixtures (ported from the standalone tcsh-dap project) ---

import shutil  # noqa: E402
import stat  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def tcsh_path() -> Path:
    found = shutil.which("tcsh")
    if found is None:
        pytest.skip("stock tcsh is not installed")
    return Path(found)


@pytest.fixture(scope="session")
def tcsh_fixtures_dir() -> Path:
    return Path(__file__).parent / "integration" / "fixtures"


@pytest.fixture
async def dap_client() -> AsyncIterator["object"]:
    from tests.integration.tcsh_dap_client import DAPClient

    client = await DAPClient.start()
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
def basic_program(tmp_path: Path) -> Path:
    source = Path(__file__).parent / "integration" / "fixtures" / "basic.csh"
    program = tmp_path / "basic.csh"
    program.write_bytes(source.read_bytes())
    return program


@pytest.fixture
def recording_tcsh(tmp_path: Path) -> Path:
    executable = tmp_path / "recording-tcsh"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "record = {\n"
        "    'argv': sys.argv,\n"
        "    'cwd': os.getcwd(),\n"
        "    'original_0': os.environ.get('__tcsh_dap_original_0'),\n"
        "    'overlay': os.environ.get('TCSH_DAP_TEST_OVERLAY'),\n"
        "    'stdin': os.read(0, 1).decode('utf-8'),\n"
        "    'pid': os.getpid(),\n"
        "    'process_group': os.getpgrp(),\n"
        "}\n"
        "Path(os.environ['TCSH_DAP_TEST_RECORD']).write_text(json.dumps(record))\n"
        "os.write(1, b'stdout line\\nstdout partial\\xff')\n"
        "os.write(2, b'stderr line\\n')\n"
        "raise SystemExit(int(os.environ.get('TCSH_DAP_TEST_EXIT', '0')))\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable
