"""Pytest configuration.

Adds ``trading_system`` to ``sys.path`` so the absolute imports used throughout
the codebase (``from core.x import y``) resolve when pytest is invoked from
either the repository root or the package directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
