"""
demo_app/tests/test_flaky_network.py — flaky test: network category.

Calls fake_fetch(), which raises TimeoutError ~30% of the time to simulate a
flaky network call.  No real internet access is used.

FlakeHunter hint: network  (the import of app which uses random for fake network
                             is detected; scanner also sees the TimeoutError pattern)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import fake_fetch


def test_fetch_returns_ok():
    """Assert the fake API returns status 200 — fails ~30% of runs."""
    result = fake_fetch("http://example.com/api")
    assert result["status"] == 200, f"Expected 200, got {result}"
