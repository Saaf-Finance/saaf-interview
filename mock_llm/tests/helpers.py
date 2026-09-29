"""Shared helpers for the mock LLM tests."""

import json

# No latency, no random failures: tests opt in to the behaviour they exercise.
QUIET = dict(error_rate=0.0, latency_median_s=0.0, slow_tail_rate=0.0, stream_drop_rate=0.0)


class FakeClock:
    """Monotonic clock that only moves when told to, so rate-limit tests are exact."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def prompt(task=None, ticket=None, order=None, decision=None, system="You are a support agent."):
    """Build chat messages following the TASK / TICKET / ORDER / DECISION line protocol."""
    lines = []
    if task:
        lines.append(f"TASK: {task}")
    if ticket is not None:
        lines.append(f"TICKET: {json.dumps(ticket)}")
    if order is not None:
        lines.append(f"ORDER: {json.dumps(order)}")
    if decision is not None:
        lines.append(f"DECISION: {json.dumps(decision)}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines) or "hello"}]


def chat(client, messages, ticket_id="T-00001", run_id=None, **body):
    headers = {"X-Ticket-Id": ticket_id} if ticket_id else {}
    if run_id:
        headers["X-Run-Id"] = run_id
    payload = {"model": "mock-large", "messages": messages, **body}
    return client.post("/v1/chat/completions", json=payload, headers=headers)


def parse_sse(text: str) -> list[str]:
    """Return the data payloads of an SSE body, in order."""
    return [line[len("data: "):] for line in text.split("\n\n") if line.startswith("data: ")]
