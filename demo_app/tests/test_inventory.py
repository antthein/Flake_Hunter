"""Tests for the inventory management module."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import add_stock

# Shared inventory state — not reset between tests.
INVENTORY = {"widgets": 0}


def test_inventory_stock_increments():
    """Adding stock should increase the widget count by one."""
    add_stock(INVENTORY, "widgets")
    assert INVENTORY["widgets"] == 1, f"Expected 1, got {INVENTORY['widgets']}"


def test_inventory_starts_empty():
    """A fresh inventory should report zero widgets."""
    assert INVENTORY["widgets"] == 0, (
        f"Expected 0 widgets, got {INVENTORY['widgets']}"
    )
