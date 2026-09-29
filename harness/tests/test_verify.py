"""verify.compute() on synthetic ledgers, plus the normalisers that make it robust to odd responses."""

import pytest

from harness.verify import (
    CLOCK_TOLERANCE_S,
    compute,
    normalize_commerce,
    normalize_llm,
    normalize_runs,
    percentile,
    summarize_llm_entries,
)

T0 = 1_000.0


def exp(category, outcome="no_refund", amount=None, requires_approval=False, approval=None, expects_email=True):
    return {"outcome": outcome, "amount": amount, "requires_approval": requires_approval, "approval": approval,
            "approval_delay_s": 5.0 if requires_approval else None, "category": category,
            "expects_email": expects_email}


def small(amount=100.0):
    return exp("eligible_small", "refund", amount)


def large(approve=True, amount=900.0):
    return exp("eligible_large", "refund" if approve else "no_refund", amount if approve else None, True,
               "approve" if approve else "reject")


def workload(expected):
    return {"customers": [{"customer_id": "cust-01", "segment": "large"},
                          {"customer_id": "cust-02", "segment": "standard"},
                          {"customer_id": "cust-03", "segment": "standard"}],
            "expected": expected}


def submit(tid, customer="cust-02", ts=T0, accepted=True, attempts=1):
    return {"ticket_id": tid, "customer_id": customer, "first_submit_ts": ts, "accepted": accepted,
            "attempts": [{"attempt": i + 1, "ts": ts + i, "status": 202 if accepted else 503}
                         for i in range(attempts)]}


def run(tid, status="completed", finished=T0 + 5, run_id=None):
    return {"run_id": run_id or f"r-{tid}-{status}-{finished}", "ticket_id": tid, "status": status,
            "created_at": T0, "finished_at": finished if status in ("completed", "failed") else None}


def refund(tid, amount=100.0, ts=T0 + 3):
    return {"refund_id": f"rf-{tid}-{ts}", "ticket_id": tid, "order_id": "O", "amount": amount, "ts": ts}


def email(tid, ts=T0 + 4):
    return {"email_id": f"em-{tid}-{ts}", "ticket_id": tid, "ts": ts}


def observed(runs, refunds=(), emails=(), llm=None, run_errors=None, commerce=True):
    store = None
    if commerce:
        store, _ = normalize_commerce({"refunds": list(refunds), "emails": list(emails), "refund_replays": 0})
    return {"runs": runs, "run_errors": run_errors or {}, "commerce": store,
            "commerce_error": None if commerce else "HTTP 500", "llm": llm, "llm_error": None}


def llm_ledger(per_ticket_costs, by_status=None, unattributed_cost=0.0, unattributed_calls=0):
    per_ticket = {tid: {"calls": 3, "ok_calls": 3, "cost_usd": c} for tid, c in per_ticket_costs.items()}
    if unattributed_calls:
        per_ticket["None"] = {"calls": unattributed_calls, "ok_calls": unattributed_calls,
                              "cost_usd": unattributed_cost}
    calls = sum(v["calls"] for v in per_ticket.values())
    by_status = by_status or {"200": calls}
    body = {"calls": sum(by_status.values()), "by_status": by_status,
            "cost_usd": sum(v["cost_usd"] for v in per_ticket.values()), "dropped_streams": 1,
            "per_ticket": per_ticket}
    return normalize_llm(body)[0]


def test_clean_run_passes():
    expected = {"T1": small(), "T2": exp("not_refund"), "T3": large(True), "T4": exp("partially_shipped",
                                                                                     expects_email=False)}
    subs = [submit(t) for t in expected]
    runs = {t: [run(t)] for t in expected}
    obs = observed(runs, refunds=[refund("T1"), refund("T3", 900.0, ts=T0 + 20)],
                   emails=[email("T1"), email("T2"), email("T3")])
    m = compute(subs, workload(expected), obs, {"T3": {"decision": True, "approved_ts": T0 + 19}})
    assert m["hard"] == dict.fromkeys(m["hard"], 0)
    assert m["passed"] is True
    assert set(m["checks"]["hard"].values()) == {"PASS"}
    assert m["hard"]["duplicate_runs"] == 0
    assert m["soft"]["duplicate_emails"] == 0
    assert m["soft"]["missing_emails"] == 0
    assert m["flagged"] == {}


def test_lost_runs():
    expected = {t: small() for t in ("T1", "T2", "T3", "T4", "T5")}
    runs = {
        "T1": [run("T1")],
        "T2": [],                                   # never created
        "T3": [run("T3", "running")],               # stuck
        "T4": [run("T4", "awaiting_approval")],     # still waiting
        # T5: the lookup failed
    }
    obs = observed(runs, refunds=[refund("T1")], emails=[email("T1")], run_errors={"T5": "HTTP 503"})
    m = compute([submit(t) for t in expected], workload(expected), obs)
    assert m["hard"]["lost_runs"] == 4
    assert sorted(m["flagged"]["lost_runs"]) == ["T2", "T3", "T4", "T5"]
    assert m["hard"]["missing_refunds"] == 0  # only finished runs can be missing a refund
    assert m["soft"]["missing_emails"] == 0
    assert m["checks"]["hard"]["lost_runs"] == "FAIL" and m["passed"] is False


def test_failed_run_is_terminal_not_lost():
    expected = {"T1": exp("partially_shipped", expects_email=False)}
    m = compute([submit("T1")], workload(expected), observed({"T1": [run("T1", "failed")]}))
    assert m["hard"]["lost_runs"] == 0


def test_status_is_case_insensitive_and_malformed_records_are_ignored():
    expected = {"T1": exp("not_refund")}
    runs, _ = normalize_runs({"runs": ["junk", 3, {"run_id": "a", "ticket_id": "T1", "status": "COMPLETED",
                                                    "finished_at": T0 + 1}]}, "T1")
    m = compute([submit("T1")], workload(expected), observed({"T1": runs}, emails=[email("T1")]))
    assert m["hard"]["lost_runs"] == 0


def test_duplicate_refunds_sum_extra_refunds():
    expected = {"T1": small(), "T2": small(), "T3": small()}
    runs = {t: [run(t)] for t in expected}
    refunds = [refund("T1")] * 3 + [refund("T2")] * 2 + [refund("T3")]
    m = compute([submit(t) for t in expected], workload(expected), observed(runs, refunds=refunds))
    assert m["hard"]["duplicate_refunds"] == 3
    assert sorted(m["flagged"]["duplicate_refunds"]) == ["T1", "T2"]


def test_unexpected_refunds():
    expected = {"T1": exp("final_sale"), "T2": exp("not_refund"), "T3": large(approve=False), "T4": small()}
    runs = {t: [run(t)] for t in expected}
    refunds = [refund("T1"), refund("T2"), refund("T2"), refund("T3", 900.0), refund("T4")]
    approvals = {"T3": {"decision": False, "approved_ts": None}}
    m = compute([submit(t) for t in expected], workload(expected), observed(runs, refunds=refunds), approvals)
    assert m["hard"]["unexpected_refunds"] == 3  # counted per ticket
    assert m["hard"]["duplicate_refunds"] == 1
    assert m["hard"]["refund_before_approval"] == 1  # T3 was rejected, so it was never approved


def test_missing_refunds():
    expected = {"T1": small(), "T2": small(), "T3": small(), "T4": large(True)}
    runs = {"T1": [run("T1")], "T2": [run("T2", "failed")], "T3": [run("T3", "running")], "T4": [run("T4")]}
    m = compute([submit(t) for t in expected], workload(expected), observed(runs),
                {"T4": {"decision": True, "approved_ts": T0 + 10}})
    assert m["hard"]["missing_refunds"] == 3  # T1, T2 and T4; T3 is lost instead
    assert sorted(m["flagged"]["missing_refunds"]) == ["T1", "T2", "T4"]


def test_wrong_amount_refunds():
    expected = {"T1": small(100.0), "T2": small(100.0), "T3": small(100.0), "T4": small(100.0)}
    runs = {t: [run(t)] for t in expected}
    refunds = [refund("T1", 99.99), refund("T2", 100.004), {"ticket_id": "T3", "amount": "lots", "ts": T0},
               refund("T4", 100.0)]
    m = compute([submit(t) for t in expected], workload(expected), observed(runs, refunds=refunds))
    assert m["hard"]["wrong_amount_refunds"] == 2
    assert sorted(m["flagged"]["wrong_amount_refunds"]) == ["T1", "T3"]


def test_refund_before_approval():
    expected = {t: large(True) for t in ("T1", "T2", "T3", "T4", "T5")}
    runs = {t: [run(t, finished=T0 + 30)] for t in expected}
    refunds = [
        refund("T1", 900.0, ts=T0 + 21),                         # after approval: fine
        refund("T2", 900.0, ts=T0 + 5),                          # before approval
        refund("T3", 900.0, ts=T0 + 20 - CLOCK_TOLERANCE_S / 2),  # within clock tolerance: fine
        refund("T4", 900.0, ts=T0 + 21),                         # never approved
        refund("T5", 900.0, ts=T0 + 25),                         # approval "sent" but never delivered
    ]
    approvals = {t: {"decision": True, "approved_ts": T0 + 20} for t in ("T1", "T2", "T3")}
    approvals["T5"] = {"decision": True, "approved_ts": None}
    m = compute([submit(t) for t in expected], workload(expected), observed(runs, refunds=refunds), approvals)
    assert m["hard"]["refund_before_approval"] == 3
    assert sorted(m["flagged"]["refund_before_approval"]) == ["T2", "T4", "T5"]
    assert m["hard"]["unexpected_refunds"] == 0


def test_duplicate_runs_and_run_dedup():
    expected = {"T1": exp("not_refund"), "T2": exp("not_refund")}
    runs_t1, _ = normalize_runs({"runs": [run("T1", run_id="a"), run("T1", run_id="a")]}, "T1")
    runs_t2 = [run("T2", "failed", run_id="b"), run("T2", run_id="c")]
    m = compute([submit(t) for t in expected], workload(expected),
                observed({"T1": runs_t1, "T2": runs_t2}, emails=[email("T1"), email("T2")]))
    assert m["hard"]["duplicate_runs"] == 1
    assert m["flagged"]["duplicate_runs"] == ["T2"]


def test_duplicate_and_missing_emails():
    expected = {
        "T1": exp("not_refund"),                              # 3 emails -> 2 duplicates
        "T2": exp("final_sale"),                              # 0 emails, finished -> missing
        "T3": exp("partially_shipped", expects_email=False),  # 0 emails, not expected
        "T4": exp("not_refund"),                              # 0 emails but lost -> not counted as missing
        "T5": exp("partially_shipped", expects_email=False),  # 1 email, not expected: no metric
    }
    runs = {"T1": [run("T1")], "T2": [run("T2")], "T3": [run("T3")], "T4": [], "T5": [run("T5")]}
    emails = [email("T1"), email("T1"), email("T1"), email("T5")]
    m = compute([submit(t) for t in expected], workload(expected), observed(runs, emails=emails))
    assert m["soft"]["duplicate_emails"] == 2
    assert m["soft"]["missing_emails"] == 1
    assert m["flagged"]["missing_emails"] == ["T2"]


def test_percentile_nearest_rank():
    values = list(range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 99) == 99
    assert percentile([3.0], 95) == 3.0
    assert percentile([], 95) is None
    assert percentile([1, 2, 3, 4], 50) == 2


def test_latency_uses_first_submit_and_earliest_completed_run():
    expected = {"T1": exp("not_refund"), "T2": exp("not_refund"), "T3": exp("not_refund"), "T4": large(True),
                "T5": exp("not_refund")}
    runs = {
        "T1": [run("T1", finished=T0 + 4)],
        # a failed attempt at +2, then a completed run at +9 and another at +12: the ticket was done at +9
        "T2": [run("T2", "failed", T0 + 2), run("T2", "completed", T0 + 12), run("T2", "completed", T0 + 9)],
        "T3": [run("T3", "failed", T0 + 6)],                  # only failed: counts when it failed
        "T4": [run("T4", finished=T0 + 100)],                 # needs approval: excluded
        "T5": [{"run_id": "x", "ticket_id": "T5", "status": "completed", "finished_at": None}],  # no timestamp
    }
    subs = [submit(t, ts=T0) for t in expected]
    subs[0]["first_submit_ts"] = T0 + 1  # T1 submitted later: 3 s
    m = compute(subs, workload(expected), observed(runs, emails=[email(t) for t in expected]),
                {"T4": {"decision": True, "approved_ts": T0}})
    lat = m["soft"]["latency_automated_s"]
    assert lat["n"] == 3
    rows = {r["ticket_id"]: r["latency_s"] for r in m["tickets"]}
    assert rows["T1"] == 3.0 and rows["T2"] == 9.0 and rows["T3"] == 6.0 and rows["T5"] is None
    assert (lat["p50"], lat["p95"], lat["p99"]) == (6.0, 9.0, 9.0)


def test_latency_percentiles_and_soft_check():
    expected = {f"T{i}": exp("not_refund") for i in range(1, 101)}
    runs = {f"T{i}": [run(f"T{i}", finished=T0 + i)] for i in range(1, 101)}
    m = compute([submit(t) for t in expected], workload(expected), observed(runs))
    lat = m["soft"]["latency_automated_s"]
    assert (lat["n"], lat["p50"], lat["p95"], lat["p99"]) == (100, 50.0, 95.0, 99.0)
    assert m["checks"]["soft"]["latency_p95_automated_s"] == "missed"
    runs = {f"T{i}": [run(f"T{i}", finished=T0 + i / 10)] for i in range(1, 101)}
    m = compute([submit(t) for t in expected], workload(expected), observed(runs))
    assert m["checks"]["soft"]["latency_p95_automated_s"] == "met"


def test_per_customer_p95_and_worst_small_customer():
    expected, subs, runs = {}, [], {}
    spec = {"cust-01": [1, 2, 3, 50], "cust-02": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
                                                    40], "cust-03": [2, 30]}
    n = 0
    for customer, latencies in spec.items():
        for latency in latencies:
            n += 1
            tid = f"T{n}"
            expected[tid] = exp("not_refund")
            subs.append(submit(tid, customer=customer))
            runs[tid] = [run(tid, finished=T0 + latency)]
    m = compute(subs, workload(expected), observed(runs))
    per = m["soft"]["per_customer_p95_s"]
    assert per["cust-01"] == {"n": 4, "p95_s": 50.0}
    assert per["cust-02"] == {"n": 20, "p95_s": 19.0}
    assert per["cust-03"] == {"n": 2, "p95_s": 30.0}
    assert m["soft"]["worst_small_customer_p95"] == {"customer_id": "cust-03", "n": 2, "p95_s": 30.0}
    assert m["soft"]["large_account_p95"] == {"cust-01": {"n": 4, "p95_s": 50.0}}


def test_llm_429_rate_and_5xx():
    expected = {"T1": exp("not_refund")}
    obs = observed({"T1": [run("T1")]}, llm=llm_ledger({"T1": 0.01}, by_status={"200": 85, "429": 10, "500": 2,
                                                                                   "503": 3}))
    m = compute([submit("T1")], workload(expected), obs)
    llm = m["soft"]["llm"]
    assert llm["calls"] == 100 and llm["calls_429"] == 10 and llm["calls_5xx"] == 5
    assert llm["rate_429"] == pytest.approx(0.10)
    assert m["checks"]["soft"]["llm_429_rate"] == "missed"
    obs["llm"] = llm_ledger({"T1": 0.01}, by_status={"200": 96, "429": 4})
    m = compute([submit("T1")], workload(expected), obs)
    assert m["soft"]["llm"]["rate_429"] == pytest.approx(0.04)
    assert m["checks"]["soft"]["llm_429_rate"] == "met"


def test_cost_tails_and_partially_shipped_split():
    expected = {f"T{i}": exp("not_refund") for i in range(1, 19)}
    expected["P1"] = exp("partially_shipped", expects_email=False)
    expected["P2"] = exp("partially_shipped", expects_email=False)
    costs = {f"T{i}": i / 1000 for i in range(1, 19)}  # T1..T18: 0.001 .. 0.018
    costs["P1"] = 0.5
    costs["P2"] = 0.3
    del costs["T18"]  # no LLM calls recorded for T18 -> cost 0
    subs = [submit(t) for t in expected]
    obs = observed({t: [run(t)] for t in expected},
                   llm=llm_ledger(costs, unattributed_cost=0.25, unattributed_calls=7))
    m = compute(subs, workload(expected), obs)
    cost, llm = m["soft"]["cost"], m["soft"]["llm"]
    per_ticket = sorted(list(costs.values()) + [0.0])
    assert cost["per_ticket_max_usd"] == 0.5
    assert cost["per_ticket_p50_usd"] == percentile(per_ticket, 50) == 0.009
    assert cost["per_ticket_p95_usd"] == percentile(per_ticket, 95) == 0.3
    assert cost["partially_shipped_mean_usd"] == pytest.approx(0.4)
    assert cost["partially_shipped_tickets"] == 2
    assert cost["others_mean_usd"] == pytest.approx(sum(i / 1000 for i in range(1, 18)) / 18)
    assert cost["total_usd"] == pytest.approx(sum(costs.values()) + 0.25)
    assert llm["unattributed_calls"] == 7
    assert llm["unattributed_cost_usd"] == pytest.approx(0.25)
    assert llm["dropped_streams"] == 1


def test_store_ledger_unavailable_fails_refund_checks():
    expected = {"T1": small()}
    m = compute([submit("T1")], workload(expected), observed({"T1": [run("T1")]}, commerce=False))
    assert m["hard"]["lost_runs"] == 0
    for name in ("duplicate_refunds", "unexpected_refunds", "missing_refunds", "wrong_amount_refunds",
                 "refund_before_approval"):
        assert m["hard"][name] is None
        assert m["checks"]["hard"][name] == "FAIL"
    assert m["soft"]["duplicate_emails"] is None and m["soft"]["missing_emails"] is None
    assert m["passed"] is False


def test_llm_ledger_unavailable():
    expected = {"T1": exp("not_refund")}
    m = compute([submit("T1")], workload(expected), observed({"T1": [run("T1")]}, emails=[email("T1")]))
    assert m["soft"]["llm"] == {"available": False}
    assert m["checks"]["soft"]["llm_429_rate"] == "n/a"
    assert m["passed"] is True


def test_submit_counts_and_extra_refunds():
    expected = {"T1": small(), "T2": small(), "T9": small()}
    subs = [submit("T1", attempts=3), submit("T2", accepted=False, attempts=5)]
    refunds = [refund("T1"), refund("T9"), {"amount": 1.0, "ts": T0}]
    m = compute(subs, workload(expected), observed({"T1": [run("T1")]}, refunds=refunds))
    assert m["soft"]["submits"] == {"tickets": 2, "attempts": 8, "failed_tickets": 1}
    assert m["info"]["refunds_for_unsubmitted_tickets"] == 1
    assert m["info"]["refunds_without_ticket_id"] == 1
    assert m["hard"]["unexpected_refunds"] == 0
    assert m["hard"]["lost_runs"] == 1


def test_normalize_runs_shapes():
    assert normalize_runs("nope", "T1") == ([], "response has no 'runs' list")
    assert normalize_runs({"items": []}, "T1")[1] is not None
    runs, err = normalize_runs([{"run_id": 1, "ticket_id": "T1"}, {"run_id": 2, "ticket_id": "T2"},
                                {"run_id": 3}], "T1")
    assert err is None and [r["run_id"] for r in runs] == [1, 3]


def test_normalize_commerce_and_llm_reject_garbage():
    assert normalize_commerce({"refunds": "x", "emails": []})[0] is None
    assert normalize_commerce(None)[0] is None
    store, _ = normalize_commerce({"refunds": [None, {"ticket_id": 5, "amount": "12.5", "ts": "nan"}], "emails": []})
    assert store["refunds"] == [{"refund_id": None, "ticket_id": "5", "order_id": None, "amount": 12.5, "ts": None}]
    assert store["malformed_entries"] == 1
    assert normalize_llm({"detail": "Not Found"})[0] is None
    assert normalize_llm([1, 2])[0] is None
    assert summarize_llm_entries({"nope": 1})[0] is None


def test_raw_llm_entries_match_summary():
    import fakes

    llm = fakes.FakeLLM()
    for tid, status in [("T1", 200), ("T1", 429), ("T2", 200), (None, 503), ("T2", 200)]:
        llm.record(tid, status)
    from_summary, _ = normalize_llm(llm.ledger())
    from_raw, _ = summarize_llm_entries({"entries": llm.entries})
    assert from_raw["calls"] == from_summary["calls"] == 5
    assert from_raw["by_status"] == from_summary["by_status"]
    assert from_raw["cost_usd"] == pytest.approx(from_summary["cost_usd"])
    for tid in ("T1", "T2"):
        assert from_raw["per_ticket"][tid]["calls"] == from_summary["per_ticket"][tid]["calls"]
        assert from_raw["per_ticket"][tid]["cost_usd"] == pytest.approx(from_summary["per_ticket"][tid]["cost_usd"])
