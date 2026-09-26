"""
demo_app/tests/test_flaky_random.py — flaky test: random category.

Calls random_winner(["a", "b", "c"]) and asserts the result is in ["a", "b"].
Fails ~33% of the time (when "c" is chosen).

FlakeHunter hint: random  (import random + random.choice via random_winner detected by scanner)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import random_winner


def test_random_winner_is_acceptable():
    """Assert the winner is one of the expected values — fails ~33% of runs."""
    result = random_winner(["a", "b", "c"])
    assert result in ["a", "b"], f"Got unexpected winner: {result!r}"
