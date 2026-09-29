"""The TASK / TICKET / ORDER / DECISION protocol and the answers built from it."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mockllm.tasks import (
    FALLBACK_REPLY, TaskInput, assess, classify, draft_reply, parse_task_input, respond,
)

NEED_INFO = "Delivery date for part of this order is unknown; I need to check the order again."


# --- parsing ------------------------------------------------------------------

def test_parse_reads_lines_anywhere_in_the_message():
    text = (
        "Please handle this ticket.\n"
        "TASK: assess\n"
        'TICKET: {"ticket_id": "T-1", "order_id": "O-1", "message": "hi"}\n'
        'ORDER: {"order_id": "O-1", "days_since_delivery": 3}\n'
        'DECISION: {"outcome": "refunded"}\n'
        "Answer with JSON only."
    )
    inp = parse_task_input(text)
    assert inp.task == "assess"
    assert inp.ticket == {"ticket_id": "T-1", "order_id": "O-1", "message": "hi"}
    assert inp.order == {"order_id": "O-1", "days_since_delivery": 3}
    assert inp.decision == {"outcome": "refunded"}


def test_parse_tolerates_missing_or_broken_json():
    inp = parse_task_input("TASK: classify\nTICKET: {not json\nORDER: [1, 2]")
    assert inp.task == "classify"
    assert inp.ticket == {} and inp.order == {} and inp.decision == {}


def test_parse_requires_task_at_line_start():
    assert parse_task_input("the TASK: classify line is quoted here").task is None


@pytest.mark.parametrize("text", ["hello there", "TASK: summarize\nTICKET: {}", "TASK:   \n"])
def test_unknown_or_missing_task_gets_fallback(text):
    assert respond(parse_task_input(text)) == FALLBACK_REPLY


# --- classify -----------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "I want a refund please",
    "How do I RETURN this jacket?",
    "Can I get my money back?",
    "The lamp arrived damaged",
    "My headphones are broken",
    "You sent me the wrong item",
    "The blender is defective",
    "This shirt doesn't fit",
    "The coat does not fit me",
])
def test_classify_refund_keywords(message):
    out = json.loads(classify({"ticket_id": "T-00042", "message": message}))
    assert out["intent"] == "refund_request"
    assert 0.80 <= out["confidence"] <= 0.99


@pytest.mark.parametrize("message", [
    "Where is my order?",
    "Can I change my shipping address?",
    "Do you ship to Canada?",
    "",
])
def test_classify_other_questions(message):
    out = json.loads(classify({"ticket_id": "T-00043", "message": message}))
    assert out["intent"] == "not_refund"
    assert 0.80 <= out["confidence"] <= 0.99


def test_classify_missing_ticket_is_not_refund():
    assert json.loads(classify({}))["intent"] == "not_refund"


def test_classify_is_deterministic_and_keys_are_exact():
    ticket = {"ticket_id": "T-00007", "message": "refund"}
    assert classify(ticket) == classify(ticket)
    assert set(json.loads(classify(ticket))) == {"intent", "confidence"}


# --- assess -------------------------------------------------------------------

@pytest.mark.parametrize("order, decision, reason", [
    ({"status": "partially_shipped", "days_since_delivery": None, "final_sale": False},
     "need_more_info", NEED_INFO),
    ({"status": "partially_shipped", "days_since_delivery": 5, "final_sale": True},
     "need_more_info", NEED_INFO),
    ({"status": "delivered", "days_since_delivery": None, "final_sale": False},
     "need_more_info", NEED_INFO),
    ({"status": "delivered", "final_sale": False},  # key missing entirely
     "need_more_info", NEED_INFO),
    ({}, "need_more_info", NEED_INFO),
    ({"status": "delivered", "days_since_delivery": 45, "final_sale": True},
     "ineligible", "Final-sale items can't be refunded."),
    ({"status": "delivered", "days_since_delivery": 3, "final_sale": True},
     "ineligible", "Final-sale items can't be refunded."),
    ({"status": "delivered", "days_since_delivery": 31, "final_sale": False},
     "ineligible", "Outside the 30-day return window."),
    ({"status": "delivered", "days_since_delivery": 90, "final_sale": False},
     "ineligible", "Outside the 30-day return window."),
    ({"status": "delivered", "days_since_delivery": 30, "final_sale": False},
     "eligible", "Within the return window."),
    ({"status": "delivered", "days_since_delivery": 1, "final_sale": False},
     "eligible", "Within the return window."),
])
def test_assess_rules(order, decision, reason):
    assert json.loads(assess(order)) == {"decision": decision, "reason": reason}


# --- draft_reply --------------------------------------------------------------

OUTCOMES = ["refunded", "declined", "not_refund", "escalated", "something_else", None]


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_draft_reply_length_and_order_id(outcome):
    for i in range(1, 201):
        ticket = {"ticket_id": f"T-{i:05d}", "order_id": f"O-{i:05d}", "message": "refund"}
        order = {"order_id": f"O-{i:05d}", "amount": 123.45}
        decision = {"outcome": outcome, "reason": "Outside the 30-day return window."}
        text = draft_reply(ticket, order, decision)
        assert 300 <= len(text) <= 900, (outcome, i, len(text))
        assert f"O-{i:05d}" in text
        with pytest.raises(ValueError):
            json.loads(text)


def test_draft_reply_wording_depends_on_outcome():
    ticket = {"ticket_id": "T-00001", "order_id": "O-00001"}
    order = {"order_id": "O-00001", "amount": 80.0}
    replies = {o: draft_reply(ticket, order, {"outcome": o}) for o in OUTCOMES[:4]}
    assert len(set(replies.values())) == 4
    assert "refund" in replies["refunded"] and "$80.00" in replies["refunded"]
    assert "not able to offer a refund" in replies["declined"] or "can't" in replies["declined"]


def test_draft_reply_includes_decline_reason():
    text = draft_reply({"ticket_id": "T-00009", "order_id": "O-9"}, {},
                       {"outcome": "declined", "reason": "Final-sale items can't be refunded."})
    assert "Final-sale items can't be refunded." in text


def test_draft_reply_long_reason_stays_within_limit():
    text = draft_reply({"ticket_id": "T-1", "order_id": "O-1"}, {},
                       {"outcome": "declined", "reason": "x" * 5000})
    assert 300 <= len(text) <= 900


def test_draft_reply_uses_header_ticket_id_when_ticket_has_none():
    decision = {"outcome": "refunded"}
    a = draft_reply({"order_id": "O-1"}, {}, decision, ticket_id="T-00001")
    assert a == draft_reply({"ticket_id": "T-00001", "order_id": "O-1"}, {}, decision)


def test_draft_reply_is_stable_across_processes():
    """Built-in hash() is salted per process; replies must not depend on it."""
    code = (
        "from mockllm.tasks import draft_reply\n"
        "print(draft_reply({'ticket_id': 'T-00123', 'order_id': 'O-00123'}, {}, "
        "{'outcome': 'declined'}))"
    )
    root = Path(__file__).resolve().parents[1]
    outputs = set()
    for hash_seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(root)}
        outputs.add(subprocess.run([sys.executable, "-c", code], env=env, check=True,
                                   capture_output=True, text=True).stdout)
    assert len(outputs) == 1


def test_respond_dispatch():
    ticket = {"ticket_id": "T-1", "order_id": "O-1", "message": "it's broken"}
    order = {"order_id": "O-1", "days_since_delivery": 2, "final_sale": False,
             "status": "delivered"}
    assert json.loads(respond(TaskInput("classify", ticket)))["intent"] == "refund_request"
    assert json.loads(respond(TaskInput("assess", ticket, order)))["decision"] == "eligible"
    assert "O-1" in respond(TaskInput("draft_reply", ticket, order, {"outcome": "refunded"}))
