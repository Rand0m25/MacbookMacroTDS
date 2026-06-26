"""Deterministic lint gate: pyflakes + ruff must stay clean.

This catches the whole class of unused/undefined/redefinition/import-order issues
in one pass (which the LLM reviews don't flag) and keeps it from regressing.
Skips when the tools aren't installed.
"""

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(mod_args):
    return subprocess.run([sys.executable, "-m", *mod_args], cwd=REPO,
                          capture_output=True, text=True)


def test_pyflakes_clean():
    pytest.importorskip("pyflakes")
    r = _run(["pyflakes", "tds_macro", "tests"])
    assert r.returncode == 0, "pyflakes findings:\n" + r.stdout + r.stderr


def test_ruff_clean():
    pytest.importorskip("ruff")
    r = _run(["ruff", "check", "tds_macro", "tests"])
    assert r.returncode == 0, "ruff findings:\n" + r.stdout + r.stderr
