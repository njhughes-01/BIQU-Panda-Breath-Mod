"""Shared pytest fixtures.

Panda.py derives BASE_DIR from its own ``__file__`` and, at import time, builds
PREHEAT_STATE_FILE = BASE_DIR / "preheat_state.json" and immediately calls
_load_preheat_state(). Tests that load Panda.py therefore have to be redirected
*before* the module body runs; assigning ``mod.PREHEAT_STATE_FILE`` afterwards is
too late (the import-time read has already happened) and leaves any test that
forgets to override it writing to the repo's real runtime state file.

``panda_state_dir`` gives every test its own empty directory. ``load_panda`` in
test_panda_cc2_integration.py executes the real Panda.py source with ``__file__``
pointed into that directory, so BASE_DIR, PREHEAT_STATE_FILE and the debug-log
handler all resolve there.
"""
import pytest

_current_state_dir = None


def current_state_dir():
    """Directory that replaces Panda.py's BASE_DIR for the running test."""
    if _current_state_dir is None:
        raise RuntimeError(
            "No per-test state dir is active; refusing to load Panda.py against "
            "the repository's real preheat_state.json."
        )
    return _current_state_dir


@pytest.fixture(autouse=True)
def panda_state_dir(tmp_path):
    """Per-test directory standing in for Panda.py's BASE_DIR."""
    global _current_state_dir
    _current_state_dir = tmp_path
    try:
        yield tmp_path
    finally:
        _current_state_dir = None
