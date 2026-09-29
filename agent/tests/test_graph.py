"""Workflow routing, with the LLM and store backend replaced by in-process fakes."""

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from app.config import RECURSION_LIMIT
from app.graph import build_graph

from .fakes import TICKETS


@pytest.fixture
def graph():
    return build_graph(InMemorySaver())


def cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}


def start(graph, name: str, thread_id: str | None = None) -> dict:
    thread_id = thread_id or f"run-{name}"
    return graph.invoke({"ticket": TICKETS[name], "lookups": 0}, cfg(thread_id))


def test_build_graph_requires_a_checkpointer():
    with pytest.raises(TypeError):
        build_graph()
    for missing in (None, False):
        with pytest.raises(ValueError, match="checkpointer"):
            build_graph(missing)


def test_not_refund_skips_order_lookup_and_replies(graph, fake_llm, fake_commerce):
    result = start(graph, "question")

    assert result["intent"] == "not_refund"
    assert result["outcome"] == "not_refund"
    assert fake_llm.tasks() == ["classify", "draft_reply"]
    assert fake_llm.calls[-1]["protocol"]["DECISION"] == {"outcome": "not_refund"}
    assert "ORDER" not in fake_llm.calls[-1]["protocol"]
    assert fake_commerce.refunds == []
    assert [e["to"] for e in fake_commerce.emails] == ["cust-07@example.com"]
    assert result["email_id"] == fake_commerce.emails[0]["email_id"]


def test_eligible_small_refund_is_issued_immediately(graph, fake_llm, fake_commerce):
    result = start(graph, "small")

    assert result["decision"] == "eligible"
    assert result["outcome"] == "refunded"
    assert result["lookups"] == 1
    assert fake_llm.tasks() == ["classify", "assess", "draft_reply"]
    assert len(fake_commerce.refunds) == 1
    refund = fake_commerce.refunds[0]
    assert refund["ticket_id"] == "T-O-SMALL"
    assert refund["order_id"] == "O-SMALL"
    assert refund["amount"] == 120.0
    assert result["refund_id"] == refund["refund_id"]
    assert fake_llm.calls[-1]["protocol"]["DECISION"]["outcome"] == "refunded"
    assert len(fake_commerce.emails) == 1
    assert fake_commerce.emails[0]["ticket_id"] == "T-O-SMALL"


def test_eligible_large_refund_pauses_then_approved(graph, fake_llm, fake_commerce):
    first = start(graph, "large", "run-large-ok")

    assert "__interrupt__" in first
    assert first["__interrupt__"][0].value == {
        "type": "approval", "ticket_id": "T-O-LARGE", "order_id": "O-LARGE", "amount": 1250.0,
    }
    assert fake_commerce.refunds == []
    assert fake_commerce.emails == []

    final = graph.invoke(Command(resume={"approved": True, "approver": "alice"}), cfg("run-large-ok"))

    assert "__interrupt__" not in final
    assert final["approval"] == {"approved": True, "approver": "alice"}
    assert final["outcome"] == "refunded"
    assert [r["amount"] for r in fake_commerce.refunds] == [1250.0]
    assert len(fake_commerce.emails) == 1


def test_eligible_large_refund_pauses_then_rejected(graph, fake_llm, fake_commerce):
    first = start(graph, "large", "run-large-no")
    assert "__interrupt__" in first

    final = graph.invoke(Command(resume={"approved": False, "approver": "bob"}), cfg("run-large-no"))

    assert final["approval"] == {"approved": False, "approver": "bob"}
    assert final["outcome"] == "declined"
    assert fake_commerce.refunds == []
    assert fake_llm.calls[-1]["protocol"]["DECISION"]["outcome"] == "declined"
    assert len(fake_commerce.emails) == 1


@pytest.mark.parametrize("name, reason", [
    ("final", "Final-sale items can't be refunded."),
    ("old", "Outside the 30-day return window."),
])
def test_ineligible_orders_are_declined(graph, fake_llm, fake_commerce, name, reason):
    result = start(graph, name)

    assert result["decision"] == "ineligible"
    assert result["outcome"] == "declined"
    assert result["reason"] == reason
    assert fake_commerce.refunds == []
    assert fake_llm.calls[-1]["protocol"]["DECISION"] == {"outcome": "declined", "reason": reason}
    assert len(fake_commerce.emails) == 1


def test_need_more_info_rechecks_order_until_recursion_limit(graph, fake_llm, fake_commerce):
    with pytest.raises(GraphRecursionError):
        start(graph, "partial", "run-partial")

    state = graph.get_state(cfg("run-partial")).values
    assert state["decision"] == "need_more_info"
    # 200 node executions: classify, then lookup_order/assess alternating (100 lookups, 99 assessments)
    assert state["lookups"] == RECURSION_LIMIT // 2
    assert fake_llm.tasks().count("assess") == RECURSION_LIMIT // 2 - 1
    assert fake_commerce.refunds == []
    assert fake_commerce.emails == []


def test_prompts_use_one_line_json_protocol(graph, fake_llm, fake_commerce):
    start(graph, "small")

    for call in fake_llm.calls:
        messages = call["payload"]["messages"]
        assert messages[0]["role"] == "system"
        assert "30 days" in messages[0]["content"]
        user = messages[-1]
        assert user["role"] == "user"
        lines = user["content"].splitlines()
        assert f"TASK: {call['task']}" in lines
        ticket_line = next(line for line in lines if line.startswith("TICKET: "))
        assert json.loads(ticket_line[len("TICKET: "):]) == TICKETS["small"]

    assess_lines = fake_llm.calls[1]["payload"]["messages"][-1]["content"].splitlines()
    order_line = next(line for line in assess_lines if line.startswith("ORDER: "))
    assert json.loads(order_line[len("ORDER: "):])["order_id"] == "O-SMALL"


def test_llm_calls_carry_ticket_and_run_headers(graph, fake_llm, fake_commerce):
    start(graph, "small", "run-headers")

    for call in fake_llm.calls:
        assert call["headers"]["x-ticket-id"] == "T-O-SMALL"
        assert call["headers"]["x-run-id"] == "run-headers"
