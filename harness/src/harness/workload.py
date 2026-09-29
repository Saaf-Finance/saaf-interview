"""Generate the ticket workload: customers, orders, tickets and the expected outcome of every ticket.

    uv run --project harness python -m harness.workload --seed 42 --out harness/data/workload.json

The same seed always produces a byte-identical file. The store backend serves `orders` from this file, the harness
submits `tickets` from it and checks results against `expected`.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from harness import DEFAULT_WORKLOAD_PATH

POOL_SIZE = 1200
LARGE_ACCOUNT = "cust-01"
LARGE_ACCOUNT_SHARE = 0.35
APPROVAL_THRESHOLD = 500.0
APPROVE_SHARE = 0.8
APPROVAL_DELAY_S = (3.0, 15.0)

# Share of the pool per category. Counts are exact (rounded), the order is a seeded shuffle.
CATEGORY_MIX = {
    "eligible_small": 0.45,
    "eligible_large": 0.12,
    "final_sale": 0.06,
    "outside_window": 0.07,
    "not_refund": 0.28,
    "partially_shipped": 0.02,
}

# Words that make a message a refund request (the fake LLM classifies on the same list).
REFUND_KEYWORDS = (
    "refund", "return", "money back", "damaged", "broken",
    "wrong item", "defective", "doesn't fit", "does not fit",
)

CUSTOMER_NAMES = [
    "Brightline Office Supply", "Ava Chen", "Liam Patel", "Sofia Rossi", "Noah Kim",
    "Mia Johnson", "Lucas Martin", "Emma Novak", "Ethan Brooks", "Zoe Adams",
    "Omar Haddad", "Isla Murphy", "Mateo Garcia", "Hana Sato", "Leo Fischer",
    "Nora Lindqvist", "Arjun Mehta", "Chloe Dubois", "Samuel Okafor", "Grace Wilson",
]

SMALL_PRODUCTS = [
    "wireless headphones", "running shoes", "coffee grinder", "desk lamp", "backpack", "yoga mat",
    "bluetooth speaker", "winter jacket", "blender", "cast iron skillet", "hiking boots",
    "mechanical keyboard", "electric kettle", "water bottle", "sunglasses", "tablet stand",
    "rain jacket", "wool sweater", "kitchen knife set", "phone case",
]
LARGE_PRODUCTS = [
    "espresso machine", "standing desk", "robot vacuum", "gaming monitor", "office chair",
    "camera lens", "air purifier", "e-bike battery", "sofa", "laptop",
]
PLACES = ["Canada", "Mexico", "the UK", "Australia", "Ireland", "Germany", "Japan", "Alaska", "Puerto Rico"]

REFUND_TEMPLATES = [
    "The {product} I ordered arrived damaged. I'd like a refund, please.",
    "My {product} stopped working after two days. It seems defective. Can I return it?",
    "I received the wrong item. I ordered a {product} and got something else entirely.",
    "I'd like to return the {product} from order {order_id}. It doesn't fit.",
    "The {product} came broken in the box. How do I get a refund?",
    "Could I get a refund for the {product}? It isn't what I expected.",
    "The {product} does not fit my space. Can I send it back for a refund?",
    "I changed my mind about the {product}. How do I return my order?",
    "The {product} arrived with a cracked panel, so it's damaged. Please refund me.",
    "You sent the wrong item instead of my {product}. I want my money back.",
    "Is it possible to return the {product}? It's not what I needed.",
    "The {product} from order {order_id} is defective. I'd like my money back.",
]
PARTIAL_TEMPLATES = [
    "Only part of order {order_id} showed up and the {product} is missing. I'd like a refund for it.",
    "Half of my order arrived but the {product} never came. Can I get my money back?",
    "My order was split and the {product} part never arrived. Please refund it.",
    "I got one box of order {order_id}, but not the {product}. I want to return what came and get a refund.",
]
QUESTION_TEMPLATES = [
    "Where is my order? I bought a {product} last week and it hasn't arrived yet.",
    "Can I change the delivery address for my {product} order?",
    "Do you ship to {place}?",
    "Is the {product} available in another color?",
    "What's the tracking number for order {order_id}?",
    "Can I add gift wrapping to my {product} order?",
    "When will the {product} be in stock again?",
    "I need to change my address for future orders. How do I do that?",
    "Do you offer express shipping to {place}?",
    "Can I pay for the {product} in installments?",
    "How long does delivery usually take to {place}?",
    "Could you update the phone number on my account?",
]
GREETINGS = ["", "", "Hi! ", "Hello. ", "Hey there! ", "Good morning. "]
SIGN_OFFS = ["", "", " Thanks!", " Thank you.", " Appreciate the help."]


def is_refund_worded(message: str) -> bool:
    text = message.lower()
    return any(word in text for word in REFUND_KEYWORDS)


def _category_list(rng: random.Random, size: int) -> list[str]:
    counts = {name: round(share * size) for name, share in CATEGORY_MIX.items()}
    counts["eligible_small"] += size - sum(counts.values())  # absorb rounding
    categories = [name for name, n in counts.items() for _ in range(n)]
    rng.shuffle(categories)
    return categories


def _amount(rng: random.Random, large: bool) -> float:
    return round(rng.uniform(520.0, 2400.0), 2) if large else round(rng.uniform(15.0, 480.0), 2)


def _message(rng: random.Random, templates: list[str], product: str, order_id: str) -> str:
    body = rng.choice(templates).format(product=product, order_id=order_id, place=rng.choice(PLACES))
    return f"{rng.choice(GREETINGS)}{body}{rng.choice(SIGN_OFFS)}"


def generate(seed: int = 42, size: int = POOL_SIZE) -> dict:
    """Build the whole workload dict for a seed."""
    rng = random.Random(seed)
    customers = [
        {
            "customer_id": f"cust-{i:02d}",
            "name": name,
            "email": f"cust-{i:02d}@example.com",
            "segment": "large" if f"cust-{i:02d}" == LARGE_ACCOUNT else "standard",
        }
        for i, name in enumerate(CUSTOMER_NAMES, start=1)
    ]
    others = [c["customer_id"] for c in customers if c["customer_id"] != LARGE_ACCOUNT]
    categories = _category_list(rng, size)

    orders: dict[str, dict] = {}
    tickets: list[dict] = []
    expected: dict[str, dict] = {}
    for i, category in enumerate(categories, start=1):
        ticket_id, order_id = f"T-{i:05d}", f"O-{i:05d}"
        customer_id = LARGE_ACCOUNT if rng.random() < LARGE_ACCOUNT_SHARE else rng.choice(others)

        if category == "eligible_small":
            large = False
        elif category == "eligible_large":
            large = True
        else:
            large = rng.random() < 0.25
        amount = _amount(rng, large)
        product = rng.choice(LARGE_PRODUCTS if large else SMALL_PRODUCTS)
        days: int | None = rng.randint(1, 29)
        status = "delivered"
        if category == "outside_window":
            days = rng.randint(31, 90)
        elif category == "partially_shipped":
            days, status = None, "partially_shipped"
        orders[order_id] = {
            "order_id": order_id,
            "customer_id": customer_id,
            "amount": amount,
            "currency": "USD",
            "days_since_delivery": days,
            "final_sale": category == "final_sale",
            "status": status,
        }

        templates = {"not_refund": QUESTION_TEMPLATES, "partially_shipped": PARTIAL_TEMPLATES}.get(
            category, REFUND_TEMPLATES)
        message = _message(rng, templates, product, order_id)
        if is_refund_worded(message) != (category != "not_refund"):
            raise AssertionError(f"message wording does not match category {category}: {message!r}")
        tickets.append({
            "ticket_id": ticket_id,
            "customer_id": customer_id,
            "order_id": order_id,
            "email": f"{customer_id}@example.com",
            "message": message,
        })

        exp = {
            "outcome": "no_refund",
            "amount": None,
            "requires_approval": False,
            "approval": None,
            "approval_delay_s": None,
            "category": category,
            "expects_email": category != "partially_shipped",
        }
        if category == "eligible_small":
            exp.update(outcome="refund", amount=amount)
        elif category == "eligible_large":
            approve = rng.random() < APPROVE_SHARE
            exp.update(
                requires_approval=True,
                approval="approve" if approve else "reject",
                approval_delay_s=round(rng.uniform(*APPROVAL_DELAY_S), 2),
            )
            if approve:
                exp.update(outcome="refund", amount=amount)
        expected[ticket_id] = exp

    return {"seed": seed, "customers": customers, "orders": orders, "tickets": tickets, "expected": expected}


def dumps(workload: dict) -> str:
    return json.dumps(workload, indent=1) + "\n"


def load(path: str | Path = DEFAULT_WORKLOAD_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=DEFAULT_WORKLOAD_PATH)
    args = parser.parse_args(argv)
    workload = generate(args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(dumps(workload), encoding="utf-8")
    mix: dict[str, int] = {}
    for exp in workload["expected"].values():
        mix[exp["category"]] = mix.get(exp["category"], 0) + 1
    print(f"wrote {len(workload['tickets'])} tickets to {args.out}: {mix}")


if __name__ == "__main__":
    main()
