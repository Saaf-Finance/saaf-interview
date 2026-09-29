"""Chat-completions client and the prompts the agent uses.

The user message always carries the ticket context as one-line JSON, one item per line:

    TASK: <classify | assess | draft_reply>
    TICKET: {...}
    ORDER: {...}       (when an order has been looked up)
    DECISION: {...}    (draft_reply only)
"""

import json
import logging
import time

import httpx

from . import config

log = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""You are a friendly customer support agent for an online store.
You help customers with returns and refunds and answer general questions about their orders.

Return policy:
- Items can be returned for a full refund within {config.RETURN_WINDOW_DAYS} days of delivery.
- Final-sale items cannot be refunded.
- Refunds over ${config.APPROVAL_THRESHOLD:.0f} need a manager's approval before they are issued.

Follow the TASK line in the user message and reply in the requested format."""

CLASSIFY_INSTRUCTIONS = (
    "Decide whether the customer is asking for a refund or return. "
    'Reply with JSON only: {"intent": "refund_request" or "not_refund", "confidence": number between 0 and 1}.'
)
ASSESS_INSTRUCTIONS = (
    "Check the request against the return policy using the order details. "
    'Reply with JSON only: {"decision": "eligible" or "ineligible" or "need_more_info", "reason": short explanation}.'
)
DRAFT_REPLY_INSTRUCTIONS = (
    "Write the email reply to the customer based on the decision. "
    "Plain text only, friendly and concise, and mention their order number."
)

_http = httpx.Client(timeout=config.LLM_TIMEOUT_S)


class LLMError(RuntimeError):
    """The LLM could not be reached or returned something we can't use."""


def build_user_message(task: str, ticket: dict, order: dict | None = None,
                       decision: dict | None = None, instructions: str = "") -> str:
    """Render the user message: optional instructions, then the TASK/TICKET/ORDER/DECISION lines."""
    lines = [instructions, ""] if instructions else []
    lines.append(f"TASK: {task}")
    lines.append(f"TICKET: {json.dumps(ticket)}")
    if order is not None:
        lines.append(f"ORDER: {json.dumps(order)}")
    if decision is not None:
        lines.append(f"DECISION: {json.dumps(decision)}")
    return "\n".join(lines)


def chat(task: str, ticket: dict, *, order: dict | None = None, decision: dict | None = None,
         instructions: str = "", max_tokens: int = 256, run_id: str | None = None) -> str:
    """Send one chat completion request and return the assistant's text."""
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(task, ticket, order, decision, instructions)},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    headers = {"X-Ticket-Id": ticket["ticket_id"]}
    if run_id:
        headers["X-Run-Id"] = run_id
    url = f"{config.LLM_BASE_URL}/chat/completions"

    last_error: Exception | None = None
    for attempt in range(1, config.LLM_MAX_ATTEMPTS + 1):
        try:
            resp = _http.post(url, json=payload, headers=headers)
            if resp.status_code >= 429:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            last_error = exc
            log.warning("LLM %s attempt %d/%d failed: %s", task, attempt, config.LLM_MAX_ATTEMPTS, exc)
            if attempt < config.LLM_MAX_ATTEMPTS:
                time.sleep(config.LLM_RETRY_DELAY_S)
    raise LLMError(f"LLM call '{task}' failed after {config.LLM_MAX_ATTEMPTS} attempts: {last_error}")


def _parse_json(task: str, content: str) -> dict:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError(f"could not parse {task} response as JSON: {content[:200]!r}") from exc
    if not isinstance(data, dict):
        raise LLMError(f"expected a JSON object from {task}, got: {content[:200]!r}")
    return data


def classify(ticket: dict, run_id: str | None = None) -> dict:
    """Return {"intent": "refund_request" | "not_refund", "confidence": float}."""
    content = chat("classify", ticket, instructions=CLASSIFY_INSTRUCTIONS, max_tokens=64, run_id=run_id)
    return _parse_json("classify", content)


def assess(ticket: dict, order: dict, run_id: str | None = None) -> dict:
    """Return {"decision": "eligible" | "ineligible" | "need_more_info", "reason": str}."""
    content = chat("assess", ticket, order=order, instructions=ASSESS_INSTRUCTIONS,
                   max_tokens=128, run_id=run_id)
    return _parse_json("assess", content)


def draft_reply(ticket: dict, order: dict | None, decision: dict, run_id: str | None = None) -> str:
    """Return the plain-text email body for the customer."""
    content = chat("draft_reply", ticket, order=order, decision=decision,
                   instructions=DRAFT_REPLY_INSTRUCTIONS, max_tokens=512, run_id=run_id)
    return content.strip()
