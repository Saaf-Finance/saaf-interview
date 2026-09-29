"""In-process fakes for the LLM provider and the store backend, served through httpx.MockTransport."""

import json
import re
import uuid

import httpx

REFUND_WORDS = ("refund", "return", "money back", "damaged", "broken", "wrong item",
                "defective", "doesn't fit", "does not fit")

LINE_RE = {key: re.compile(rf"^{key}: (.*)$", re.MULTILINE) for key in ("TASK", "TICKET", "ORDER", "DECISION")}


def parse_protocol(text: str) -> dict:
    """Pull the TASK/TICKET/ORDER/DECISION lines out of a user message (JSON-decoded where relevant)."""
    parsed = {}
    for key, pattern in LINE_RE.items():
        match = pattern.search(text)
        if match:
            parsed[key] = match.group(1) if key == "TASK" else json.loads(match.group(1))
    return parsed


class FakeLLM:
    """A tiny chat-completions endpoint that answers the agent's three tasks deterministically."""

    def __init__(self):
        self.calls: list[dict] = []          # {"task", "headers", "payload", "protocol"}
        self.fail_next: list[int] = []       # status codes to return before answering normally

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protocol = parse_protocol(payload["messages"][-1]["content"])
        task = protocol.get("TASK")
        self.calls.append({"task": task, "headers": dict(request.headers), "payload": payload, "protocol": protocol})
        if self.fail_next:
            status = self.fail_next.pop(0)
            return httpx.Response(status, json={"error": {"type": "error", "message": "try again"}},
                                  headers={"Retry-After": "3"})
        return httpx.Response(200, json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": payload["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": self._answer(task, protocol)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        })

    @staticmethod
    def _answer(task: str | None, protocol: dict) -> str:
        if task == "classify":
            text = protocol["TICKET"]["message"].lower()
            intent = "refund_request" if any(w in text for w in REFUND_WORDS) else "not_refund"
            return json.dumps({"intent": intent, "confidence": 0.9})
        if task == "assess":
            order = protocol["ORDER"]
            if order["status"] == "partially_shipped" or order["days_since_delivery"] is None:
                return json.dumps({"decision": "need_more_info", "reason": "Delivery date unknown."})
            if order["final_sale"]:
                return json.dumps({"decision": "ineligible", "reason": "Final-sale items can't be refunded."})
            if order["days_since_delivery"] > 30:
                return json.dumps({"decision": "ineligible", "reason": "Outside the 30-day return window."})
            return json.dumps({"decision": "eligible", "reason": "Within the return window."})
        if task == "draft_reply":
            outcome = protocol["DECISION"]["outcome"]
            return f"Hi there, about order {protocol['TICKET']['order_id']}: {outcome}. Thanks for shopping with us."
        return "I can help with that. Could you share more details?"

    def tasks(self) -> list[str]:
        return [c["task"] for c in self.calls]


class FakeCommerce:
    """Orders, refunds and emails kept in memory."""

    def __init__(self, orders: dict[str, dict]):
        self.orders = orders
        self.refunds: list[dict] = []
        self.emails: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.refund_behaviour: list[str] = []   # per attempt: "ok" | "503" | "timeout_after_record"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.startswith("/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            if order is None:
                return httpx.Response(404, json={"error": "order not found"})
            return httpx.Response(200, json=order)
        if request.method == "POST" and path == "/refunds":
            behaviour = self.refund_behaviour.pop(0) if self.refund_behaviour else "ok"
            if behaviour == "503":
                return httpx.Response(503, json={"error": "temporarily unavailable"})
            body = json.loads(request.content)
            refund = {"refund_id": f"rf_{uuid.uuid4().hex[:12]}", **body}
            self.refunds.append(refund)
            if behaviour == "timeout_after_record":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(201, json={"refund_id": refund["refund_id"], "status": "issued"})
        if request.method == "POST" and path == "/emails":
            body = json.loads(request.content)
            email = {"email_id": f"em_{uuid.uuid4().hex[:12]}", **body}
            self.emails.append(email)
            return httpx.Response(201, json={"email_id": email["email_id"]})
        return httpx.Response(404, json={"error": "not found"})

    def refund_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/refunds"]


ORDERS = {
    "O-SMALL": {"order_id": "O-SMALL", "customer_id": "cust-02", "amount": 120.0, "currency": "USD",
                "days_since_delivery": 10, "final_sale": False, "status": "delivered"},
    "O-LARGE": {"order_id": "O-LARGE", "customer_id": "cust-03", "amount": 1250.0, "currency": "USD",
                "days_since_delivery": 5, "final_sale": False, "status": "delivered"},
    "O-FINAL": {"order_id": "O-FINAL", "customer_id": "cust-04", "amount": 80.0, "currency": "USD",
                "days_since_delivery": 3, "final_sale": True, "status": "delivered"},
    "O-OLD": {"order_id": "O-OLD", "customer_id": "cust-05", "amount": 60.0, "currency": "USD",
              "days_since_delivery": 45, "final_sale": False, "status": "delivered"},
    "O-PARTIAL": {"order_id": "O-PARTIAL", "customer_id": "cust-06", "amount": 300.0, "currency": "USD",
                  "days_since_delivery": None, "final_sale": False, "status": "partially_shipped"},
    "O-QUESTION": {"order_id": "O-QUESTION", "customer_id": "cust-07", "amount": 45.0, "currency": "USD",
                   "days_since_delivery": 2, "final_sale": False, "status": "delivered"},
}


def ticket(order_id: str, message: str, ticket_id: str | None = None) -> dict:
    order = ORDERS[order_id]
    return {
        "ticket_id": ticket_id or f"T-{order_id}",
        "customer_id": order["customer_id"],
        "order_id": order_id,
        "email": f"{order['customer_id']}@example.com",
        "message": message,
    }


TICKETS = {
    "small": ticket("O-SMALL", "The mug arrived broken, can I get a refund?"),
    "large": ticket("O-LARGE", "I'd like to return the laptop, it doesn't fit my needs."),
    "final": ticket("O-FINAL", "Please refund the scarf, I changed my mind."),
    "old": ticket("O-OLD", "Can I return these shoes? They were the wrong item."),
    "partial": ticket("O-PARTIAL", "Only half of my order came and one item is damaged, refund please."),
    "question": ticket("O-QUESTION", "Where is my package? Do you ship to Canada?"),
}
