"""Pytest configuration for review tests."""

import sys
from pathlib import Path

# Add src and tests to path
src_path = Path(__file__).parent.parent / "src"
tests_path = Path(__file__).parent

if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))
if str(tests_path) not in sys.path:
    sys.path.insert(0, str(tests_path))