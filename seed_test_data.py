"""
Creates fake payments in a Stripe SANDBOX so the sync has something to copy.

No real money moves: it only works with a test key, and it pays with Stripe's
built-in test cards. Run it once (or whenever you want more sample data):

    STRIPE_API_KEY=sk_test_... python seed_test_data.py 12
"""

import random
import sys
import uuid

import requests

from sync import STRIPE_API, TIMEOUT, get_test_key

# Stripe's ready-made test payment methods (they only exist in test mode)
TEST_CARDS = ["pm_card_visa", "pm_card_mastercard", "pm_card_amex", "pm_card_discover"]
PRODUCTS = [("Website audit", 4900), ("Monthly maintenance", 9900),
            ("Data cleanup", 15000), ("Setup fee", 2500), ("Consulting hour", 7500)]


def post(session, path, data):
    # An idempotency key means a retried request can never create a duplicate payment
    resp = session.post(f"{STRIPE_API}/{path}", data=data, timeout=TIMEOUT,
                        headers={"Idempotency-Key": str(uuid.uuid4())})
    resp.raise_for_status()
    return resp.json()


def main(count):
    session = requests.Session()
    session.auth = (get_test_key(), "")
    for i in range(1, count + 1):
        name, cents = random.choice(PRODUCTS)
        intent = post(session, "payment_intents", {
            "amount": cents,
            "currency": "usd",
            "description": name,
            "receipt_email": f"test.customer{i}@example.com",
            "payment_method": random.choice(TEST_CARDS),
            "confirm": "true",
            "automatic_payment_methods[enabled]": "true",
            "automatic_payment_methods[allow_redirects]": "never",
        })
        print(f"{i}/{count}: {name} ${cents / 100:.2f} -> {intent['status']}")
        if i % 5 == 0:                      # refund every 5th one so refunds show up too
            post(session, "refunds", {"payment_intent": intent["id"]})
            print("   refunded")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10)
