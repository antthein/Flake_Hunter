"""
demo_app/app.py — tiny sample application used by the demo test suite.

Four functions:
  - increment(counter_dict, key)  : mutates a shared dict (enables shared-state flakiness)
  - random_winner(items)          : returns random.choice(items) with no seed
  - fake_fetch(url)               : simulates a ~30% flaky network call (no real I/O)
  - compute(x, y)                 : pure arithmetic used by stable tests
"""

import random


def increment(counter_dict: dict, key: str) -> None:
    """Add 1 to counter_dict[key] in-place."""
    counter_dict[key] = counter_dict.get(key, 0) + 1


def random_winner(items: list) -> object:
    """Return a random element from items (no seed — intentionally non-deterministic)."""
    return random.choice(items)


def fake_fetch(url: str) -> dict:
    """
    Simulate a flaky HTTP GET.

    Returns {"status": 200} ~70% of the time.
    Raises TimeoutError ~30% of the time to simulate a network hiccup.
    No real network I/O is performed.
    """
    if random.random() < 0.30:
        raise TimeoutError("simulated timeout")
    return {"status": 200}


def compute(x: float, y: float) -> float:
    """Return x + y. Pure and deterministic."""
    return x + y
