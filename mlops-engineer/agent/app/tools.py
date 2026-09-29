"""Calls to the store backend: order lookup, refunds and customer emails."""

import logging
import time

import httpx

from . import config

log = logging.getLogger(__name__)

_http = httpx.Client(timeout=config.TOOL_TIMEOUT_S)


class ToolError(RuntimeError):
    """A store backend call failed."""


def _call(method: str, path: str, json: dict | None = None) -> httpx.Response:
    """Make a request, retrying on timeouts, connection errors and 5xx responses."""
    url = f"{config.COMMERCE_URL}{path}"
    last_error = ""
    for attempt in range(1, config.TOOL_MAX_ATTEMPTS + 1):
        try:
            resp = _http.request(method, url, json=json)
        except httpx.TransportError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code < 500:
                return resp
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        log.warning("%s %s attempt %d/%d failed: %s", method, path, attempt, config.TOOL_MAX_ATTEMPTS, last_error)
        if attempt < config.TOOL_MAX_ATTEMPTS:
            time.sleep(config.TOOL_RETRY_DELAY_S)
    raise ToolError(f"{method} {path} failed after {config.TOOL_MAX_ATTEMPTS} attempts: {last_error}")


def _json_or_raise(resp: httpx.Response, what: str) -> dict:
    if not resp.is_success:
        raise ToolError(f"{what} failed: HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def lookup_order(order_id: str) -> dict:
    """Fetch an order: {order_id, customer_id, amount, currency, days_since_delivery, final_sale, status}."""
    resp = _call("GET", f"/orders/{order_id}")
    if resp.status_code == 404:
        raise ToolError(f"order {order_id} not found")
    return _json_or_raise(resp, f"lookup of order {order_id}")


def issue_refund(ticket_id: str, order_id: str, amount: float) -> dict:
    """Issue a refund for the full order amount. Returns {refund_id, status}."""
    resp = _call("POST", "/refunds", json={"ticket_id": ticket_id, "order_id": order_id, "amount": amount})
    return _json_or_raise(resp, f"refund for order {order_id}")


def send_email(ticket_id: str, to: str, subject: str, body: str) -> dict:
    """Send an email to the customer. Returns {email_id}."""
    resp = _call("POST", "/emails", json={"ticket_id": ticket_id, "to": to, "subject": subject, "body": body})
    return _json_or_raise(resp, f"email to {to}")
