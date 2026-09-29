"""HTTP API for the support agent.

POST /runs runs the workflow for one ticket and returns the run record once the run has finished
or paused for approval. POST /runs/{run_id}/approve resumes a paused run with the manager's decision.
"""

import logging
import time
import uuid

from fastapi import FastAPI, HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel

from .config import RECURSION_LIMIT
from .graph import build_graph

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("agent")

app = FastAPI(title="Support agent")

checkpointer = InMemorySaver()
graph = build_graph(checkpointer)

RUNS: dict[str, dict] = {}


class TicketIn(BaseModel):
    ticket_id: str
    customer_id: str
    order_id: str
    email: str
    message: str


class ApprovalIn(BaseModel):
    approved: bool
    approver: str


def _graph_config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}, "recursion_limit": RECURSION_LIMIT}


def _summarize(values: dict) -> dict:
    """The parts of the final graph state that are useful to API clients."""
    order = values.get("order") or {}
    return {
        "outcome": values.get("outcome"),
        "intent": values.get("intent"),
        "decision": values.get("decision"),
        "reason": values.get("reason"),
        "order_id": values["ticket"]["order_id"],
        "amount": order.get("amount"),
        "approval": values.get("approval"),
        "refund_id": values.get("refund_id"),
        "email_id": values.get("email_id"),
        "lookups": values.get("lookups", 0),
    }


def _execute(run: dict, graph_input) -> None:
    """Drive the graph until it finishes, pauses at an interrupt, or raises. Updates `run` in place."""
    run_id = run["run_id"]
    config = _graph_config(run_id)
    try:
        paused_for = None
        for update in graph.stream(graph_input, config, stream_mode="updates"):
            for node_name, value in update.items():
                if node_name == "__interrupt__":
                    paused_for = value[0].value
                else:
                    run["steps"] += 1
        if paused_for is not None:
            run["status"] = "awaiting_approval"
            run["result"] = {"approval_request": paused_for}
            log.info("run %s (%s) awaiting approval", run_id, run["ticket_id"])
            return
        run["result"] = _summarize(graph.get_state(config).values)
        run["status"] = "completed"
        run["finished_at"] = time.time()
        log.info("run %s (%s) completed: %s", run_id, run["ticket_id"], run["result"]["outcome"])
    except Exception as exc:
        run["status"] = "failed"
        run["error"] = str(exc)
        run["finished_at"] = time.time()
        log.exception("run %s (%s) failed", run_id, run["ticket_id"])


def _get_run(run_id: str) -> dict:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/runs")
def create_run(ticket: TicketIn) -> dict:
    run_id = str(uuid.uuid4())
    run = {
        "run_id": run_id,
        "ticket_id": ticket.ticket_id,
        "customer_id": ticket.customer_id,
        "status": "running",
        "created_at": time.time(),
        "finished_at": None,
        "error": None,
        "result": None,
        "steps": 0,
    }
    RUNS[run_id] = run
    log.info("run %s started for ticket %s", run_id, ticket.ticket_id)
    _execute(run, {"ticket": ticket.model_dump(), "lookups": 0})
    return run


@app.get("/runs")
def list_runs(ticket_id: str | None = None, status: str | None = None) -> dict:
    runs = [
        run for run in list(RUNS.values())
        if (ticket_id is None or run["ticket_id"] == ticket_id)
        and (status is None or run["status"] == status)
    ]
    return {"runs": runs}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return _get_run(run_id)


@app.post("/runs/{run_id}/approve")
def approve_run(run_id: str, approval: ApprovalIn) -> dict:
    run = _get_run(run_id)
    if run["status"] in ("completed", "failed"):
        return run
    if run["status"] != "awaiting_approval":
        raise HTTPException(status_code=409, detail=f"run is {run['status']}, not awaiting approval")
    log.info("run %s %s by %s", run_id, "approved" if approval.approved else "rejected", approval.approver)
    run["status"] = "running"
    _execute(run, Command(resume={"approved": approval.approved, "approver": approval.approver}))
    return run
