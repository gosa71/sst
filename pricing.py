"""
pricing.py — FastAPI pricing example used in SST README.

Run this file to record a baseline:
    sst record pricing.py

Then verify after any code change:
    sst verify pricing.py
"""

from sst.core import sst


@sst.capture
def calculate_price(product_id: str, quantity: int, user_tier: str = "standard") -> dict:
    """Calculate the final price for a product order.

    This is the business logic layer — decorated with @sst.capture so SST
    records real inputs and outputs as a behavioral baseline.
    """
    prices = {"SKU-001": 99.9, "SKU-002": 249.0, "SKU-003": 19.9}
    discounts = {"premium": 0.15, "standard": 0.0, "trial": 0.05}

    base = prices.get(product_id, 0.0)
    if base == 0.0:
        raise ValueError(f"Unknown product: {product_id}")

    discount_rate = discounts.get(user_tier, 0.0)
    subtotal = round(base * quantity, 2)
    discount_amount = round(subtotal * discount_rate, 2)
    total = round(subtotal - discount_amount, 2)

    return {
        "product_id": product_id,
        "quantity": quantity,
        "unit_price": base,
        "subtotal": subtotal,
        "discount": discount_amount,
        "total": total,
        "currency": "USD",
    }


if __name__ == "__main__":
    # These calls are captured when you run:  sst record pricing.py
    # Each unique combination of inputs becomes one baseline scenario.
    calculate_price("SKU-001", 1, "standard")   # → total: 99.9
    calculate_price("SKU-001", 2, "premium")    # → total: 169.83 (15% off)
    calculate_price("SKU-002", 1, "standard")   # → total: 249.0
    calculate_price("SKU-003", 5, "trial")      # → total: 94.525 (5% off)
