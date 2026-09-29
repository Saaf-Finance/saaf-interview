"""What the mock model answers.

The model reads structured lines from the last user message:

    TASK: classify | assess | draft_reply
    TICKET: {"ticket_id": ..., "order_id": ..., "message": ...}
    ORDER: {"order_id": ..., "amount": ..., "days_since_delivery": ..., "final_sale": ..., "status": ...}
    DECISION: {"outcome": ..., "reason": ...}

Answers are deterministic: the same input always produces the same output.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

FALLBACK_REPLY = "I can help with that. Could you share more details?"
RETURN_WINDOW_DAYS = 30

REFUND_KEYWORDS = (
    "refund", "return", "money back", "damaged", "broken",
    "wrong item", "defective", "doesn't fit", "does not fit",
)

_LINE_RE = {
    name: re.compile(rf"^{name}:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
    for name in ("TASK", "TICKET", "ORDER", "DECISION")
}


@dataclass
class TaskInput:
    task: str | None = None
    ticket: dict[str, Any] = field(default_factory=dict)
    order: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] = field(default_factory=dict)


def message_text(content: Any) -> str:
    """Text of a message's content (a string, or a list of OpenAI-style content parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def parse_task_input(text: str) -> TaskInput:
    """Extract TASK / TICKET / ORDER / DECISION lines from a prompt."""
    found = {name: pattern.search(text) for name, pattern in _LINE_RE.items()}
    task = found["TASK"].group(1).strip().lower() if found["TASK"] else None
    return TaskInput(
        task=task or None,
        ticket=_json_object(found["TICKET"] and found["TICKET"].group(1)),
        order=_json_object(found["ORDER"] and found["ORDER"].group(1)),
        decision=_json_object(found["DECISION"] and found["DECISION"].group(1)),
    )


def respond(inp: TaskInput, ticket_id: str | None = None) -> str:
    """Return the assistant message content for a parsed prompt."""
    if inp.task == "classify":
        return classify(inp.ticket)
    if inp.task == "assess":
        return assess(inp.order)
    if inp.task == "draft_reply":
        return draft_reply(inp.ticket, inp.order, inp.decision, ticket_id)
    return FALLBACK_REPLY


# --- classify -----------------------------------------------------------------

def classify(ticket: dict[str, Any]) -> str:
    message = str(ticket.get("message") or "").lower()
    intent = "refund_request" if any(k in message for k in REFUND_KEYWORDS) else "not_refund"
    seed = _stable_hash(str(ticket.get("ticket_id") or message))
    confidence = round(0.80 + (seed % 20) / 100, 2)  # 0.80 .. 0.99
    return json.dumps({"intent": intent, "confidence": confidence})


# --- assess -------------------------------------------------------------------

def assess(order: dict[str, Any]) -> str:
    days = order.get("days_since_delivery")
    if order.get("status") == "partially_shipped" or not isinstance(days, (int, float)):
        decision, reason = "need_more_info", (
            "Delivery date for part of this order is unknown; I need to check the order again.")
    elif order.get("final_sale"):
        decision, reason = "ineligible", "Final-sale items can't be refunded."
    elif days > RETURN_WINDOW_DAYS:
        decision, reason = "ineligible", "Outside the 30-day return window."
    else:
        decision, reason = "eligible", "Within the return window."
    return json.dumps({"decision": decision, "reason": reason})


# --- draft_reply --------------------------------------------------------------

MIN_REPLY_CHARS = 300
MAX_REPLY_CHARS = 900
MAX_REASON_CHARS = 200

GREETINGS = ("Hi there,", "Hello,", "Hi,", "Dear customer,")

BODIES = {
    "refunded": (
        "Thanks for getting in touch about {order_ref}. We've issued a refund{amount} to your "
        "original payment method. Depending on your bank, it can take 5-10 business days for the "
        "money to show up on your statement. You don't need to do anything else; the refund will "
        "appear as a credit from our store.",
        "Good news: your refund for {order_ref} has been approved and processed{amount}. The funds "
        "are on their way back to the card or account you used at checkout, and most banks show "
        "the credit within 5-10 business days. We're sorry the purchase didn't work out this time.",
    ),
    "declined": (
        "Thanks for reaching out about {order_ref}. We've reviewed your request carefully, but "
        "unfortunately we're not able to offer a refund for this order. {reason}If you think we've "
        "missed something, just reply to this email with any extra details, such as photos of the "
        "item or packaging, and a member of our team will take another look.",
        "We're sorry to hear that {order_ref} didn't work out. After looking into it, we can't "
        "approve a refund in this case. {reason}We know that's disappointing. If there's anything "
        "else we can do, like helping you find a replacement or a better fit, let us know.",
    ),
    "not_refund": (
        "Thanks for your message about {order_ref}. We've passed your question to the right team. "
        "You can also check the latest status of your order from your account page at any time, "
        "and if anything about the order changes we'll send you an update by email right away.",
        "Thanks for getting in touch regarding {order_ref}. We're happy to help with your "
        "question. Most updates about shipping, delivery and account details are available in "
        "your order history, and our team will follow up if we need more information from you.",
    ),
    "escalated": (
        "Thanks for contacting us about {order_ref}. Your request needs a quick review from a "
        "senior member of our team, and we've passed it along with all the details you provided. "
        "You don't need to do anything right now; we'll email you as soon as a decision has been "
        "made, usually within one to two business days.",
        "We've received your request about {order_ref} and it's now with our specialist team for "
        "review. Some requests need a second pair of eyes before we can finalize them. We'll "
        "follow up by email as soon as the review is complete, normally within two business days.",
    ),
    "other": (
        "Thanks for contacting us about {order_ref}. We've received your message and our team is "
        "reviewing it. We'll get back to you with an update as soon as possible.",
    ),
}

CLOSINGS = (
    "Thanks for shopping with us.",
    "Thank you for your patience.",
    "We appreciate your business.",
    "Thanks again for reaching out.",
)

SIGNOFFS = (
    "Best regards,\nThe Support Team",
    "Kind regards,\nCustomer Support",
    "Warm wishes,\nThe Store Support Team",
)

FILLERS = (
    "If you have any other questions, just reply to this email and we'll be happy to help.",
    "You can find our full return policy on the help page of our website.",
)


def draft_reply(ticket: dict[str, Any], order: dict[str, Any], decision: dict[str, Any],
                ticket_id: str | None = None) -> str:
    """Plain-text customer email (300-900 chars) whose wording depends on DECISION.outcome."""
    ticket_id = str(ticket.get("ticket_id") or ticket_id or "")
    seed = _stable_hash(ticket_id)
    order_id = order.get("order_id") or ticket.get("order_id")
    outcome = str(decision.get("outcome") or "")
    bodies = BODIES.get(outcome, BODIES["other"])

    body = _pick(bodies, seed, 1).format(
        order_ref=f"order {order_id}" if order_id else "your recent order",
        amount=_amount_text(decision.get("amount", order.get("amount"))),
        reason=_reason_text(decision.get("reason")),
    )
    head = [_pick(GREETINGS, seed, 0), body]
    tail = [_pick(CLOSINGS, seed, 2), _pick(SIGNOFFS, seed, 3)]
    for filler in FILLERS:  # pad short replies up to the minimum length
        if len(_join(head + tail)) >= MIN_REPLY_CHARS:
            break
        head.append(filler)
    return _join(head + tail)[:MAX_REPLY_CHARS]


def _join(paragraphs: list[str]) -> str:
    return "\n\n".join(paragraphs)


def _pick(options: tuple[str, ...], seed: int, slot: int) -> str:
    return options[(seed >> (8 * slot)) % len(options)]


def _amount_text(amount: Any) -> str:
    if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0:
        return f" of ${amount:,.2f}"
    return ""


def _reason_text(reason: Any) -> str:
    if not isinstance(reason, str) or not reason.strip():
        return ""
    reason = reason.strip()[:MAX_REASON_CHARS].rstrip()
    if reason[-1] not in ".!?":
        reason += "."
    return f"Reason: {reason} "


def _stable_hash(value: str) -> int:
    """Process-independent hash (Python's built-in hash() is salted per process)."""
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")
