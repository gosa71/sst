import os
import random
from datetime import datetime
from sst.core import sst

# Simulate a complex domain model
class Order:
    def __init__(self, order_id, user_email, items):
        self.order_id = order_id
        self.user_email = user_email
        self.items = items
        self.created_at = datetime.now()

@sst.capture
def calculate_loyalty_points(user_data, order_details):
    """
    Calculates loyalty points based on user tier and order value.
    Contains PII (email), non-determinism (random), and complex logic.
    """
    email = user_data.get("email")
    tier = user_data.get("tier", "standard")
    total_amount = sum(item["price"] for item in order_details["items"])
    
    # Logic with some randomness/complexity
    base_points = total_amount * 0.1
    if tier == "gold":
        multiplier = 1.5
    elif tier == "silver":
        multiplier = 1.2
    else:
        multiplier = 1.0
        
    points = int(base_points * multiplier)
    
    # Simulate a "bonus" that happens occasionally
    # Fixed for deterministic baseline testing in this example
    # if random.random() > 0.8:
    #     points += 10
        
    return {
        "user": email,
        "points_earned": points,
        "timestamp": datetime.now().isoformat(),
        "transaction_id": f"TXN-{random.randint(1000, 9999)}"
    }

if __name__ == "__main__":
    # Ensure SST is enabled for this run
    # Note: Setting after import works because SSTCore.enabled is a lazy property
    os.environ["SST_ENABLED"] = "true"
    
    # Scenario 1: Gold user
    calculate_loyalty_points(
        {"email": "vip-customer@gmail.com", "tier": "gold"},
        {"items": [{"id": 1, "price": 100}]}
    )
    
    # Scenario 2: Standard user
    calculate_loyalty_points(
        {"email": "regular.joe@outlook.com", "tier": "standard"},
        {"items": [{"id": 2, "price": 50}]}
    )
    
    # Scenario 3: Silver user
    calculate_loyalty_points(
        {"email": "silver.user@company.com", "tier": "silver"},
        {"items": [{"id": 3, "price": 200}]}
    )
