"""
demo_app/tests/test_stable.py — deterministic tests that must never be flagged as flaky.

These tests prove FlakeHunter doesn't false-positive on well-written tests.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import compute


# --- arithmetic ---

def test_add_integers():
    assert compute(2, 3) == 5


def test_add_floats():
    assert compute(1.5, 2.5) == 4.0


def test_add_negative():
    assert compute(-10, 4) == -6


def test_add_zero():
    assert compute(0, 0) == 0


# --- string ops ---

def test_string_upper():
    assert "hello".upper() == "HELLO"


def test_string_strip():
    assert "  hello  ".strip() == "hello"


def test_string_split():
    assert "a,b,c".split(",") == ["a", "b", "c"]


# --- list ops ---

def test_list_slice():
    assert [1, 2, 3, 4, 5][1:3] == [2, 3]


def test_list_sort():
    result = sorted([3, 1, 2])
    assert result == [1, 2, 3]


# --- dict ops ---

def test_dict_get_existing():
    d = {"x": 42}
    assert d.get("x") == 42


def test_dict_get_missing():
    d = {}
    assert d.get("missing", 0) == 0
