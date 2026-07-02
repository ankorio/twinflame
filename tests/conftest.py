"""Shared test setup.

Makes the in-tree `synthetic` fixture module, and the `eval/` harness package,
importable from any test file without each one rewriting the sys.path dance.
"""

from __future__ import annotations

import sys
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
