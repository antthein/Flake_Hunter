"""
demo_app/tests/test_flaky_state.py — flaky test: shared-state category.

Two tests share a module-level mutable dict COUNTER.  Neither resets it before
running.  With pytest-randomly varying execution order, one of the two tests
will fail ~50% of the time depending on which runs first:

  - test_state_reset   passes only when it runs BEFORE test_state_increment
  - test_state_increment always passes (it sets the value it then checks)

FlakeHunter hint: shared-state  (module-level dict literal detected by scanner)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import increment

# Module-level shared mutable state — intentionally NOT reset between tests.
COUNTER = {"value": 0}


def test_state_increment():
    """Increment the shared counter and assert it equals 1."""
    increment(COUNTER, "value")
    assert COUNTER["value"] == 1, f"Expected 1, got {COUNTER['value']}"


def test_state_reset():
    """Assert the counter starts at 0 — fails if test_state_increment ran first."""
    assert COUNTER["value"] == 0, (
        f"Expected 0, got {COUNTER['value']} — "
        "another test mutated shared state before this one ran"
    )
