"""The ticket-handling workflow as a LangGraph StateGraph.

    START            -> classify
    classify         -> lookup_order      (refund request)
                     -> draft_reply       (anything else)
    lookup_order     -> assess
    assess           -> issue_refund      (eligible, amount <= $500)
                     -> request_approval  (eligible, amount > $500)
                     -> draft_reply       (ineligible)
                     -> lookup_order      (need_more_info: check the order again)
    request_approval -> issue_refund      (approved)
                     -> draft_reply       (rejected)
    issue_refund     -> draft_reply -> send_email -> END

Each node returns a partial state update; routing happens in the small `route_*` functions.
"""

from typing import Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import llm, tools
from .config import APPROVAL_THRESHOLD


class AgentState(TypedDict, total=False):
    ticket: dict        # {ticket_id, customer_id, order_id, email, message}
    intent: str         # "refund_request" | "not_refund"
    order: dict         # order as returned by the store backend
    decision: str       # "eligible" | "ineligible" | "need_more_info"
    reason: str
    lookups: int        # how many times the order was fetched
    approval: dict      # {"approved": bool, "approver": str}
    refund_id: str
    outcome: str        # "refunded" | "declined" | "not_refund"
    email_body: str
    email_id: str


def _run_id(config: RunnableConfig) -> str | None:
    return config.get("configurable", {}).get("thread_id")


# --- nodes -------------------------------------------------------------------------------------

def classify(state: AgentState, config: RunnableConfig) -> dict:
    result = llm.classify(state["ticket"], run_id=_run_id(config))
    intent = result.get("intent", "not_refund")
    update = {"intent": intent}
    if intent != "refund_request":
        update["outcome"] = "not_refund"
    return update


def lookup_order(state: AgentState) -> dict:
    order = tools.lookup_order(state["ticket"]["order_id"])
    return {"order": order, "lookups": state.get("lookups", 0) + 1}


def assess(state: AgentState, config: RunnableConfig) -> dict:
    result = llm.assess(state["ticket"], state["order"], run_id=_run_id(config))
    decision = result.get("decision", "ineligible")
    update = {"decision": decision, "reason": result.get("reason", "")}
    if decision == "ineligible":
        update["outcome"] = "declined"
    return update


def request_approval(state: AgentState) -> dict:
    """Pause the run until a manager approves or rejects the refund."""
    answer = interrupt({
        "type": "approval",
        "ticket_id": state["ticket"]["ticket_id"],
        "order_id": state["order"]["order_id"],
        "amount": state["order"]["amount"],
    })
    approval = {"approved": bool(answer.get("approved")), "approver": answer.get("approver")}
    update = {"approval": approval}
    if not approval["approved"]:
        update["outcome"] = "declined"
        update["reason"] = "The refund was not approved by a manager."
    return update


def issue_refund(state: AgentState) -> dict:
    ticket, order = state["ticket"], state["order"]
    refund = tools.issue_refund(ticket["ticket_id"], order["order_id"], order["amount"])
    return {"refund_id": refund["refund_id"], "outcome": "refunded"}


def draft_reply(state: AgentState, config: RunnableConfig) -> dict:
    decision = {"outcome": state["outcome"]}
    for key in ("reason", "refund_id"):
        if state.get(key):
            decision[key] = state[key]
    if state["outcome"] == "refunded":
        decision["amount"] = state["order"]["amount"]
    body = llm.draft_reply(state["ticket"], state.get("order"), decision, run_id=_run_id(config))
    return {"email_body": body}


def send_email(state: AgentState) -> dict:
    ticket = state["ticket"]
    email = tools.send_email(
        ticket["ticket_id"],
        to=ticket["email"],
        subject=f"Your request about order {ticket['order_id']}",
        body=state["email_body"],
    )
    return {"email_id": email["email_id"]}


# --- routing -----------------------------------------------------------------------------------

def route_after_classify(state: AgentState) -> Literal["lookup_order", "draft_reply"]:
    return "lookup_order" if state["intent"] == "refund_request" else "draft_reply"


def route_after_assess(state: AgentState) -> Literal["lookup_order", "issue_refund", "request_approval", "draft_reply"]:
    decision = state["decision"]
    if decision == "need_more_info":
        return "lookup_order"
    if decision == "eligible":
        if state["order"]["amount"] > APPROVAL_THRESHOLD:
            return "request_approval"
        return "issue_refund"
    return "draft_reply"


def route_after_approval(state: AgentState) -> Literal["issue_refund", "draft_reply"]:
    return "issue_refund" if state["approval"]["approved"] else "draft_reply"


# --- graph -------------------------------------------------------------------------------------

def build_graph(checkpointer: BaseCheckpointSaver):
    """Build and compile the workflow. A checkpointer is required for the approval pause."""
    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise ValueError("build_graph() requires a checkpointer: request_approval pauses the run with interrupt()")
    builder = StateGraph(AgentState)
    builder.add_node("classify", classify)
    builder.add_node("lookup_order", lookup_order)
    builder.add_node("assess", assess)
    builder.add_node("request_approval", request_approval)
    builder.add_node("issue_refund", issue_refund)
    builder.add_node("draft_reply", draft_reply)
    builder.add_node("send_email", send_email)

    builder.add_edge(START, "classify")
    builder.add_conditional_edges("classify", route_after_classify)
    builder.add_edge("lookup_order", "assess")
    builder.add_conditional_edges("assess", route_after_assess)
    builder.add_conditional_edges("request_approval", route_after_approval)
    builder.add_edge("issue_refund", "draft_reply")
    builder.add_edge("draft_reply", "send_email")
    builder.add_edge("send_email", END)
    return builder.compile(checkpointer=checkpointer)
