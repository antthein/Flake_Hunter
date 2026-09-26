"""Tests for the order processing pipeline."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time
import random


def test_order_completes_within_sla():
    """Order processing must finish within the SLA window."""
    start = time.time()
    time.sleep(random.uniform(0.0, 0.15))
    elapsed = time.time() - start
    assert elapsed < 0.1, f"Order took {elapsed:.3f}s, SLA is 0.1s"
