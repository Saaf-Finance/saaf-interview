"""LLM client and store backend client behaviour."""

import httpx
import pytest

from app import llm, tools

from .fakes import TICKETS


def test_llm_retries_rate_limit_then_succeeds(fake_llm):
    fake_llm.fail_next = [429, 503]
    result = llm.classify(TICKETS["small"], run_id="r1")

    assert result["intent"] == "refund_request"
    assert fake_llm.tasks() == ["classify"] * 3


def test_llm_gives_up_after_max_attempts(fake_llm):
    fake_llm.fail_next = [429] * 10
    with pytest.raises(llm.LLMError, match="failed after 5 attempts"):
        llm.classify(TICKETS["small"])
    assert len(fake_llm.calls) == 5


def test_llm_sends_model_and_headers(fake_llm):
    llm.assess(TICKETS["small"], {"order_id": "O-SMALL", "status": "delivered", "days_since_delivery": 3,
                                  "final_sale": False}, run_id="r2")
    call = fake_llm.calls[0]
    assert call["payload"]["model"] == "mock-large"
    assert call["payload"].get("stream") is None
    assert call["headers"]["x-ticket-id"] == "T-O-SMALL"
    assert call["headers"]["x-run-id"] == "r2"


def test_llm_non_json_answer_raises(monkeypatch, fake_llm):
    monkeypatch.setattr(fake_llm, "_answer", lambda task, protocol: "sure thing!")
    with pytest.raises(llm.LLMError, match="could not parse classify"):
        llm.classify(TICKETS["small"])


def test_user_message_lines_are_single_line_json():
    message = llm.build_user_message(
        "draft_reply", {"ticket_id": "T-1", "message": "line one\nline two"},
        order={"order_id": "O-1"}, decision={"outcome": "refunded"},
    )
    assert message.splitlines() == [
        "TASK: draft_reply",
        'TICKET: {"ticket_id": "T-1", "message": "line one\\nline two"}',
        'ORDER: {"order_id": "O-1"}',
        'DECISION: {"outcome": "refunded"}',
    ]


def test_lookup_order_not_found(fake_commerce):
    with pytest.raises(tools.ToolError, match="not found"):
        tools.lookup_order("O-missing")


def test_refund_request_body(fake_commerce):
    result = tools.issue_refund("T-9", "O-SMALL", 120.0)

    assert result["status"] == "issued"
    (request,) = fake_commerce.refund_requests()
    assert request.read() == b'{"ticket_id":"T-9","order_id":"O-SMALL","amount":120.0}'


def test_refund_retries_on_503(fake_commerce):
    fake_commerce.refund_behaviour = ["503", "ok"]
    tools.issue_refund("T-9", "O-SMALL", 120.0)

    assert len(fake_commerce.refund_requests()) == 2
    assert len(fake_commerce.refunds) == 1


def test_refund_retries_on_timeout(fake_commerce):
    fake_commerce.refund_behaviour = ["timeout_after_record", "ok"]
    tools.issue_refund("T-9", "O-SMALL", 120.0)

    assert len(fake_commerce.refund_requests()) == 2


def test_refund_gives_up_after_three_attempts(fake_commerce):
    fake_commerce.refund_behaviour = ["503"] * 3
    with pytest.raises(tools.ToolError, match="failed after 3 attempts"):
        tools.issue_refund("T-9", "O-SMALL", 120.0)


def test_email_body(fake_commerce):
    result = tools.send_email("T-9", "a@example.com", "Hello", "Body text")

    assert result["email_id"].startswith("em_")
    assert fake_commerce.emails[0]["ticket_id"] == "T-9"
    assert fake_commerce.emails[0]["to"] == "a@example.com"


def test_tool_timeout_is_two_seconds():
    assert tools._http.timeout == httpx.Timeout(2.0)
