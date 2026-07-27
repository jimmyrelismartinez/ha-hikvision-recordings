"""Runs the browser-side playback fallback suite as part of `pytest`.

The real assertions live in tests/frontend/test_fallback.html, driven by
tests/frontend/run.py — the logic under test is browser code, so a Python
reimplementation of it would only be testing the reimplementation.

Skips (rather than fails) when there is no Chrome on the box.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parent / "frontend" / "run.py"


def test_playback_falls_back_cleanly_in_a_real_browser():
    result = subprocess.run(
        [sys.executable, str(RUNNER)],
        capture_output=True, text=True, timeout=300,
    )
    if "SKIP:" in result.stdout:
        pytest.skip(result.stdout.strip())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout, result.stdout
