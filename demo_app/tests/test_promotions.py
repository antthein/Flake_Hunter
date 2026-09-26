"""Tests for the promotions engine."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import pick_promo_winner


def test_promo_winner_is_eligible():
    """Winner must be drawn from the eligible candidates."""
    result = pick_promo_winner(["alice", "bob", "carol"])
    assert result in ["alice", "bob"], f"Ineligible winner selected: {result!r}"
