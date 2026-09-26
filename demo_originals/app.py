"""Small shop application utilities."""

import random


def add_stock(inventory: dict, item: str) -> None:
    """Increment the stock count for an item."""
    inventory[item] = inventory.get(item, 0) + 1


def pick_promo_winner(candidates: list) -> object:
    """Return one candidate selected for the promotion."""
    return random.choice(candidates)


def call_payment_api(endpoint: str) -> dict:
    """Send a payment request and return the response."""
    if random.random() < 0.30:
        raise TimeoutError("simulated timeout")
    return {"status": 200}


def compute(x: float, y: float) -> float:
    """Return x + y."""
    return x + y
