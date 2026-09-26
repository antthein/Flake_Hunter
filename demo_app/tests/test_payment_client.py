"""Tests for the payment API client."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import call_payment_api


def test_payment_api_returns_ok():
    """Payment API must return HTTP 200 for a valid charge request."""
    response = call_payment_api("https://payments.internal/charge")
    assert response["status"] == 200, f"Unexpected status: {response}"
