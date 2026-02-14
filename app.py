from sst.core import sst
import time

class User:
    def __init__(self, name, email):
        self.name = name
        self.email = email
    def __repr__(self):
        return f"User(name='{self.name}', email='{self.email}')"

@sst.capture
def process_order(user, items, discount_code=None):
    """
    A complex function that simulates order processing.
    In a real scenario, this might involve DB calls, external APIs, etc.
    """
    print(f"Processing order for {user.name}...")
    time.sleep(0.5) # Simulate work
    
    total = sum(item['price'] for item in items)
    
    if discount_code == "SAVE10":
        total *= 0.9
    
    if total > 100:
        shipping = 0
    else:
        shipping = 10
        
    return {
        "user_email": user.email,
        "total_price": round(total + shipping, 2),
        "items_count": len(items),
        "free_shipping": shipping == 0
    }

if __name__ == "__main__":
    # Simulate some usage
    u1 = User("Alice", "alice@example.com")
    items1 = [{"id": 1, "price": 50}, {"id": 2, "price": 60}]
    process_order(u1, items1, discount_code="SAVE10")
    
    u2 = User("Bob", "bob@example.com")
    items2 = [{"id": 3, "price": 20}]
    process_order(u2, items2)