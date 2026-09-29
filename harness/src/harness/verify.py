"""Checks what actually happened and turns it into metrics.

`observe()` reads the service (GET /runs?ticket_id=...), the store backend's ledger and the LLM provider's ledger.
`compute()` is a pure function from those observations (plus the harness's own submit and approval records) to the
metrics on the scoreboard. Every definition lives here; harness/README.md describes them in prose.

Anything that cannot be read is treated as "not observed": a ticket whose runs can't be fetched has no terminal run
(it counts as lost), and if the store ledger can't be read the refund metrics are reported as unknown (and fail).
"""

from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from typing import Any

import httpx

TERMINAL_STATUSES = frozenset({"completed", "failed"})
AMOUNT_TOLERANCE = 0.005  # dollars
CLOCK_TOLERANCE_S = 1.0  # allowed difference between the harness clock and the store backend's clock
HARD_METRICS = (
    "lost_runs",
    "duplicate_runs",
    "duplicate_refunds",
    "unexpected_refunds",
    "missing_refunds",
    "wrong_amount_refunds",
    "refund_before_approval",
)
SOFT_TARGETS = {"llm_429_rate": 0.05, "latency_p95_automated_s": 30.0}  # value must be below the target


# --------------------------------------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------------------------------------

async def get_json(client: httpx.AsyncClient, url: str, *, params: dict | None = None, attempts: int = 1,
                   backoff_s: float = 0.5) -> tuple[Any, str | None]:
    """GET a URL and parse JSON. Returns (body, None) on success or (None, error) after all attempts fail."""
    error = "not attempted"
    for i in range(attempts):
        if i:
            await asyncio.sleep(backoff_s * i)
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"[:200]
            continue
        if resp.status_code != 200:
            error = f"HTTP {resp.status_code}"
            continue
        try:
            return resp.json(), None
        except ValueError:
            error = "response is not valid JSON"
    return None, error


def normalize_runs(body: Any, ticket_id: str) -> tuple[list[dict], str | None]:
    """Extract this ticket's run records from a GET /runs?ticket_id= response, dropping malformed entries."""
    raw = body.get("runs") if isinstance(body, dict) else body
    if not isinstance(raw, list):
        return [], "response has no 'runs' list"
    runs, seen = [], set()
    for record in raw:
        if not isinstance(record, dict):
            continue
        if record.get("ticket_id") not in (None, ticket_id):
            continue
        run_id = record.get("run_id")
        if run_id is not None:
            if str(run_id) in seen:
                continue
            seen.add(str(run_id))
        runs.append(record)
    return runs, None


async def fetch_runs(client: httpx.AsyncClient, sut_url: str, ticket_id: str,
                     attempts: int = 3) -> tuple[list[dict], str | None]:
    body, error = await get_json(client, f"{sut_url}/runs", params={"ticket_id": ticket_id}, attempts=attempts)
    if error:
        return [], error
    return normalize_runs(body, ticket_id)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _tid(value: Any) -> str | None:
    return None if value is None else str(value)


def normalize_commerce(body: Any) -> tuple[dict | None, str | None]:
    if not isinstance(body, dict) or not isinstance(body.get("refunds"), list) \
            or not isinstance(body.get("emails"), list):
        return None, "store ledger has no 'refunds'/'emails' lists"
    refunds = [
        {"refund_id": r.get("refund_id"), "ticket_id": _tid(r.get("ticket_id")), "order_id": r.get("order_id"),
         "amount": _num(r.get("amount")), "ts": _num(r.get("ts"))}
        for r in body["refunds"] if isinstance(r, dict)
    ]
    emails = [
        {"email_id": e.get("email_id"), "ticket_id": _tid(e.get("ticket_id")), "ts": _num(e.get("ts"))}
        for e in body["emails"] if isinstance(e, dict)
    ]
    malformed = len(body["refunds"]) + len(body["emails"]) - len(refunds) - len(emails)
    replays = int(_num(body.get("refund_replays")) or 0)
    return {"refunds": refunds, "emails": emails, "refund_replays": replays, "malformed_entries": malformed}, None


def normalize_llm(body: Any) -> tuple[dict | None, str | None]:
    """Normalize the LLM provider's GET /ledger summary."""
    if not isinstance(body, dict) or not ({"calls", "by_status", "per_ticket"} & body.keys()):
        return None, "LLM ledger has none of 'calls', 'by_status', 'per_ticket'"
    raw_status = body.get("by_status") if isinstance(body.get("by_status"), dict) else {}
    by_status = {str(k): int(_num(v) or 0) for k, v in raw_status.items()}
    per_ticket = {}
    raw_per_ticket = body.get("per_ticket") if isinstance(body.get("per_ticket"), dict) else {}
    for key, value in raw_per_ticket.items():
        if isinstance(value, dict):
            per_ticket[str(key)] = {
                "calls": int(_num(value.get("calls")) or 0),
                "ok_calls": int(_num(value.get("ok_calls")) or 0),
                "cost_usd": _num(value.get("cost_usd")) or 0.0,
            }
    calls = _num(body.get("calls"))
    cost = _num(body.get("cost_usd"))
    return {
        "calls": int(calls) if calls is not None else sum(by_status.values()),
        "by_status": by_status,
        "cost_usd": cost if cost is not None else sum(v["cost_usd"] for v in per_ticket.values()),
        "dropped_streams": int(_num(body.get("dropped_streams")) or 0),
        "per_ticket": per_ticket,
    }, None


def summarize_llm_entries(body: Any) -> tuple[dict | None, str | None]:
    """Build the same summary as normalize_llm() from GET /ledger/raw entries."""
    entries = body.get("entries") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        return None, "LLM raw ledger has no 'entries' list"
    by_status: dict[str, int] = defaultdict(int)
    per_ticket: dict[str, dict] = {}
    cost = 0.0
    dropped = 0
    calls = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        calls += 1
        status = entry.get("status_code")
        by_status[str(status)] += 1
        entry_cost = _num(entry.get("cost_usd")) or 0.0
        cost += entry_cost
        dropped += 1 if entry.get("dropped") else 0
        ticket = per_ticket.setdefault(str(entry.get("ticket_id")), {"calls": 0, "ok_calls": 0, "cost_usd": 0.0})
        ticket["calls"] += 1
        ticket["ok_calls"] += 1 if status == 200 else 0
        ticket["cost_usd"] += entry_cost
    return {"calls": calls, "by_status": dict(by_status), "cost_usd": cost, "dropped_streams": dropped,
            "per_ticket": per_ticket}, None


async def observe(sut_url: str, llm_url: str, commerce_url: str, ticket_ids: list[str], *,
                  concurrency: int = 16, timeout_s: float = 10.0) -> dict:
    """Read the final state of every submitted ticket and both ledgers."""
    sut_url, llm_url, commerce_url = sut_url.rstrip("/"), llm_url.rstrip("/"), commerce_url.rstrip("/")
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=timeout_s, limits=limits, trust_env=False) as client:
        semaphore = asyncio.Semaphore(concurrency)

        async def one(ticket_id: str) -> tuple[str, list[dict], str | None]:
            async with semaphore:
                runs, error = await fetch_runs(client, sut_url, ticket_id)
                return ticket_id, runs, error

        results = await asyncio.gather(*(one(t) for t in ticket_ids))

        body, commerce_error = await get_json(client, f"{commerce_url}/ledger", attempts=3)
        commerce = None
        if commerce_error is None:
            commerce, commerce_error = normalize_commerce(body)

        body, llm_error = await get_json(client, f"{llm_url}/ledger", attempts=3)
        llm = None
        if llm_error is None:
            llm, llm_error = normalize_llm(body)
        if llm is None:
            raw, raw_error = await get_json(client, f"{llm_url}/ledger/raw", attempts=2)
            if raw_error is None:
                llm, raw_error = summarize_llm_entries(raw)
            if llm is not None:
                llm_error = None
            else:
                llm_error = f"{llm_error}; raw ledger: {raw_error}"

    return {
        "runs": {tid: runs for tid, runs, _ in results},
        "run_errors": {tid: error for tid, _, error in results if error},
        "commerce": commerce,
        "commerce_error": commerce_error,
        "llm": llm,
        "llm_error": llm_error,
    }


# --------------------------------------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------------------------------------

def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least p% of the values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def _status(run: dict) -> str:
    status = run.get("status")
    return status.lower() if isinstance(status, str) else ""


def _completion_ts(terminal_runs: list[dict]) -> float | None:
    """When the ticket was done: the earliest completed run, or failing that the earliest failed run."""
    for wanted in ("completed", "failed"):
        finished = [_num(r.get("finished_at")) for r in terminal_runs if _status(r) == wanted]
        finished = [f for f in finished if f is not None]
        if finished:
            return min(finished)
    return None


def _amount_ok(actual: float | None, expected: Any) -> bool:
    expected_num = _num(expected)
    return actual is not None and expected_num is not None and abs(actual - expected_num) <= AMOUNT_TOLERANCE


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _run_summary(run: dict) -> dict:
    error = run.get("error")
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "error": str(error)[:300] if error is not None else None,
    }


def compute(submits: list[dict], workload: dict, observed: dict, approvals: dict | None = None) -> dict:
    """All metrics for one bench run.

    submits:   SubmitResult.to_dict() for every submitted ticket (needs ticket_id, customer_id, first_submit_ts,
               accepted, attempts).
    observed:  output of observe().
    approvals: Approver.per_ticket(): ticket_id -> {"decision": bool, "approved_ts": float | None, ...}.
    """
    approvals = approvals or {}
    expected = workload.get("expected", {})
    large_accounts = {c["customer_id"] for c in workload.get("customers", []) if c.get("segment") == "large"}
    large_accounts = large_accounts or {"cust-01"}
    runs_by_ticket = observed.get("runs", {})
    commerce, llm = observed.get("commerce"), observed.get("llm")

    refunds_by: dict[str | None, list[dict]] = defaultdict(list)
    emails_by: dict[str | None, list[dict]] = defaultdict(list)
    if commerce is not None:
        for refund in commerce["refunds"]:
            refunds_by[refund["ticket_id"]].append(refund)
        for email in commerce["emails"]:
            emails_by[email["ticket_id"]].append(email)

    hard: dict[str, int | None] = dict.fromkeys(HARD_METRICS, 0)
    if commerce is None:  # refund metrics need the store ledger
        for name in HARD_METRICS:
            if name not in ("lost_runs", "duplicate_runs"):
                hard[name] = None
    duplicate_emails = missing_emails = 0
    flagged: dict[str, list[str]] = defaultdict(list)
    latencies: list[tuple[str, float, str]] = []
    costs: list[tuple[str, float]] = []
    rows = []

    for submit in submits:
        tid = submit["ticket_id"]
        exp = expected.get(tid) or {}
        category = exp.get("category", "unknown")
        runs = runs_by_ticket.get(tid) or []
        terminal = [r for r in runs if _status(r) in TERMINAL_STATUSES]
        flags: list[str] = []

        if not terminal:
            hard["lost_runs"] += 1
            flags.append("lost_runs")
        if len(runs) > 1:
            hard["duplicate_runs"] += 1
            flags.append("duplicate_runs")

        refunds = refunds_by.get(tid, []) if commerce is not None else []
        emails = emails_by.get(tid, []) if commerce is not None else []
        if commerce is not None:
            wants_refund = exp.get("outcome") == "refund"
            if len(refunds) > 1:
                hard["duplicate_refunds"] += len(refunds) - 1
                flags.append("duplicate_refunds")
            if refunds and not wants_refund:
                hard["unexpected_refunds"] += 1
                flags.append("unexpected_refunds")
            if wants_refund and terminal and not refunds:
                hard["missing_refunds"] += 1
                flags.append("missing_refunds")
            if wants_refund and any(not _amount_ok(r["amount"], exp.get("amount")) for r in refunds):
                hard["wrong_amount_refunds"] += 1
                flags.append("wrong_amount_refunds")
            if exp.get("requires_approval") and refunds:
                approved_ts = (approvals.get(tid) or {}).get("approved_ts")
                refund_times = [r["ts"] for r in refunds if r["ts"] is not None]
                first_refund = min(refund_times) if refund_times else None
                if approved_ts is None or (first_refund is not None
                                           and first_refund < approved_ts - CLOCK_TOLERANCE_S):
                    hard["refund_before_approval"] += 1
                    flags.append("refund_before_approval")
            if len(emails) > 1:
                duplicate_emails += len(emails) - 1
                flags.append("duplicate_emails")
            if exp.get("expects_email") and terminal and not emails:
                missing_emails += 1
                flags.append("missing_emails")

        latency = None
        done = _completion_ts(terminal)
        first_ts = _num(submit.get("first_submit_ts"))
        if done is not None and first_ts is not None:
            latency = max(0.0, done - first_ts)
            if not exp.get("requires_approval"):
                latencies.append((submit.get("customer_id") or "?", latency, category))

        ticket_llm = (llm or {}).get("per_ticket", {}).get(tid, {})
        cost = ticket_llm.get("cost_usd", 0.0) if llm is not None else None
        if cost is not None:
            costs.append((category, cost))

        for flag in flags:
            flagged[flag].append(tid)
        rows.append({
            "ticket_id": tid,
            "customer_id": submit.get("customer_id"),
            "category": category,
            "expected_outcome": exp.get("outcome"),
            "requires_approval": bool(exp.get("requires_approval")),
            "first_submit_ts": first_ts,
            "submit_attempts": submit.get("attempts", []),
            "accepted": bool(submit.get("accepted")),
            "runs": [_run_summary(r) for r in runs],
            "run_query_error": observed.get("run_errors", {}).get(tid),
            "latency_s": round(latency, 3) if latency is not None else None,
            "refunds": [{"amount": r["amount"], "ts": r["ts"], "refund_id": r["refund_id"]} for r in refunds],
            "emails": len(emails) if commerce is not None else None,
            "approval": approvals.get(tid),
            "llm_calls": ticket_llm.get("calls", 0) if llm is not None else None,
            "cost_usd": round(cost, 6) if cost is not None else None,
            "flags": flags,
        })

    # Latency (automated tickets only) and fairness across customers.
    automated = [lat for _, lat, _ in latencies]
    # Fairness compares customers on ordinary tickets. Partially shipped orders can't be resolved automatically, so
    # their timing says nothing about how evenly customers are served; they stay in the overall percentiles above.
    by_customer: dict[str, list[float]] = defaultdict(list)
    for customer, lat, category in latencies:
        if category != "partially_shipped":
            by_customer[customer].append(lat)
    per_customer = {c: {"n": len(v), "p95_s": percentile(v, 95)} for c, v in sorted(by_customer.items())}
    small = {c: v for c, v in per_customer.items() if c not in large_accounts}
    worst_small = max(small.items(), key=lambda kv: kv[1]["p95_s"], default=None)
    large = {c: v for c, v in per_customer.items() if c in large_accounts}

    # LLM usage and cost.
    llm_metrics: dict[str, Any] = {"available": llm is not None}
    cost_metrics: dict[str, Any] = {"available": llm is not None}
    if llm is not None:
        calls = llm["calls"]
        calls_429 = llm["by_status"].get("429", 0)
        known = set(expected)
        attributed = [v for k, v in llm["per_ticket"].items() if k in known]
        partial = [c for cat, c in costs if cat == "partially_shipped"]
        others = [c for cat, c in costs if cat != "partially_shipped"]
        ticket_costs = [c for _, c in costs]
        llm_metrics.update({
            "calls": calls,
            "calls_429": calls_429,
            "rate_429": calls_429 / calls if calls else 0.0,
            "calls_5xx": sum(n for status, n in llm["by_status"].items() if status.startswith("5")),
            "by_status": llm["by_status"],
            "dropped_streams": llm["dropped_streams"],
            "calls_per_ticket_mean": calls / len(submits) if submits else None,
            "unattributed_calls": max(0, calls - sum(v["calls"] for v in attributed)),
            "unattributed_cost_usd": max(0.0, llm["cost_usd"] - sum(v["cost_usd"] for v in attributed)),
        })
        cost_metrics.update({
            "total_usd": llm["cost_usd"],
            "per_ticket_p50_usd": percentile(ticket_costs, 50),
            "per_ticket_p95_usd": percentile(ticket_costs, 95),
            "per_ticket_max_usd": max(ticket_costs) if ticket_costs else None,
            "partially_shipped_mean_usd": _mean(partial),
            "partially_shipped_total_usd": sum(partial),
            "partially_shipped_tickets": len(partial),
            "others_mean_usd": _mean(others),
            "others_total_usd": sum(others),
        })

    soft = {
        "duplicate_emails": duplicate_emails if commerce is not None else None,
        "missing_emails": missing_emails if commerce is not None else None,
        "latency_automated_s": {
            "n": len(automated),
            "p50": percentile(automated, 50),
            "p95": percentile(automated, 95),
            "p99": percentile(automated, 99),
        },
        "per_customer_p95_s": per_customer,
        "worst_small_customer_p95": (
            {"customer_id": worst_small[0], **worst_small[1]} if worst_small else None),
        "large_account_p95": {c: v for c, v in large.items()},
        "llm": llm_metrics,
        "cost": cost_metrics,
        "submits": {
            "tickets": len(submits),
            "attempts": sum(len(s.get("attempts", [])) for s in submits),
            "failed_tickets": sum(1 for s in submits if not s.get("accepted")),
        },
    }

    submitted = {s["ticket_id"] for s in submits}
    approval_required = [s["ticket_id"] for s in submits if (expected.get(s["ticket_id"]) or {}).get("requires_approval")]
    info = {
        "refunds_total": len(commerce["refunds"]) if commerce else None,
        "emails_total": len(commerce["emails"]) if commerce else None,
        "refund_replays": commerce["refund_replays"] if commerce else None,
        "refunds_for_unsubmitted_tickets": (
            sum(len(v) for k, v in refunds_by.items() if k is not None and k not in submitted) if commerce else None),
        "refunds_without_ticket_id": len(refunds_by.get(None, [])) if commerce else None,
        "emails_for_unsubmitted_tickets": (
            sum(len(v) for k, v in emails_by.items() if k is not None and k not in submitted) if commerce else None),
        "store_ledger_malformed_entries": commerce["malformed_entries"] if commerce else None,
        "run_query_errors": len(observed.get("run_errors", {})),
        "store_ledger_error": observed.get("commerce_error"),
        "llm_ledger_error": observed.get("llm_error"),
        "approvals": {
            "tickets_requiring_approval": len(approval_required),
            "seen_waiting": sum(1 for t in approval_required if t in approvals),
            "approved": sum(1 for a in approvals.values() if a.get("decision") is True),
            "rejected": sum(1 for a in approvals.values() if a.get("decision") is False),
        },
    }

    soft_values = {
        "llm_429_rate": llm_metrics.get("rate_429"),
        "latency_p95_automated_s": soft["latency_automated_s"]["p95"],
    }
    checks = {
        "hard": {name: "PASS" if value == 0 else "FAIL" for name, value in hard.items()},
        "soft": {
            name: "n/a" if soft_values[name] is None else ("met" if soft_values[name] < target else "missed")
            for name, target in SOFT_TARGETS.items()
        },
    }
    return {
        "hard": hard,
        "soft": soft,
        "info": info,
        "targets": {"hard": dict.fromkeys(HARD_METRICS, 0), "soft": dict(SOFT_TARGETS)},
        "checks": checks,
        "passed": all(v == "PASS" for v in checks["hard"].values()),
        "flagged": dict(flagged),
        "tickets": rows,
    }
