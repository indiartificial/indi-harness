"""
tests/test_imports.py

The broadest net in the suite: every harness module must compile and must
import cleanly.

This exists because of a hole found in testing. A self-modification that
broke `memory_file.py` at import time sailed through the gate, because no
guard test happened to import that module — the suite was green and the
change was merged. A test gate only catches what it actually exercises, so
a change to any file no test touches would otherwise be auto-applied
unchecked.

Keep this file cheap and universal. Anything that imports the world at
module scope, or blows up on import, fails here regardless of which module
Indi was editing.
"""

import importlib
import py_compile
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SKIP = {"__init__"}


def _modules():
    return sorted(
        p.stem for p in ROOT.glob("*.py")
        if p.stem not in SKIP and not p.stem.startswith("_")
    )


@pytest.mark.parametrize("name", _modules())
def test_module_compiles(name):
    py_compile.compile(str(ROOT / f"{name}.py"), doraise=True)


@pytest.mark.parametrize("name", _modules())
def test_module_imports(name):
    """Import-time side effects count as breakage. config.py reads the
    environment at import, so give it what it needs rather than skipping
    the module that decides every permission in the system."""
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-placeholder")
    module = importlib.import_module(name)
    assert module is not None


def test_entrypoint_exposes_main():
    import check_and_reply
    assert callable(check_and_reply.main)


def test_no_module_lost_its_docstring():
    """A cheap proxy for 'the file still looks like the file it was'. A
    truncated or half-written rewrite usually loses the header."""
    for name in _modules():
        module = importlib.import_module(name)
        assert module.__doc__, f"{name}.py has no module docstring"
