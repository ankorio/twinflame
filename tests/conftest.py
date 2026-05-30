"""Shared test setup.

Makes the in-tree `synthetic` fixture module importable from any test file
without each one rewriting the sys.path dance.
"""

from __future__ import annotations

import sys
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))
