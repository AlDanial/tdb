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
