from collections import Counter

from harness import DEFAULT_WORKLOAD_PATH
from harness import workload as wl


def test_same_seed_gives_identical_output():
    assert wl.dumps(wl.generate(42)) == wl.dumps(wl.generate(42))
    assert wl.dumps(wl.generate(7)) != wl.dumps(wl.generate(42))


def test_committed_file_matches_seed_42():
    assert DEFAULT_WORKLOAD_PATH.read_text(encoding="utf-8") == wl.dumps(wl.generate(42))


def test_cli_writes_the_same_file(tmp_path):
    out = tmp_path / "w.json"
    wl.main(["--seed", "42", "--out", str(out)])
    assert out.read_text(encoding="utf-8") == DEFAULT_WORKLOAD_PATH.read_text(encoding="utf-8")


def test_category_mix_within_two_points():
    for seed in (42, 1, 2, 3):
        workload = wl.generate(seed)
        counts = Counter(e["category"] for e in workload["expected"].values())
        total = sum(counts.values())
        assert total == wl.POOL_SIZE
        for category, share in wl.CATEGORY_MIX.items():
            assert abs(counts[category] / total - share) <= 0.02, (seed, category)


def test_shape_and_ids(workload):
    assert [c["customer_id"] for c in workload["customers"]] == [f"cust-{i:02d}" for i in range(1, 21)]
    assert workload["customers"][0]["segment"] == "large"
    tickets = workload["tickets"]
    assert [t["ticket_id"] for t in tickets] == [f"T-{i:05d}" for i in range(1, 1201)]
    assert len(workload["orders"]) == 1200
    assert set(workload["expected"]) == {t["ticket_id"] for t in tickets}
    for t in tickets:
        assert set(t) == {"ticket_id", "customer_id", "order_id", "email", "message"}
        assert t["email"] == f"{t['customer_id']}@example.com"
        assert workload["orders"][t["order_id"]]["customer_id"] == t["customer_id"]
    share = sum(t["customer_id"] == "cust-01" for t in tickets) / len(tickets)
    assert 0.30 <= share <= 0.40


def test_orders_and_expectations_agree(workload):
    approvals = Counter()
    for t in workload["tickets"]:
        order = workload["orders"][t["order_id"]]
        exp = workload["expected"][t["ticket_id"]]
        cat = exp["category"]
        assert set(order) == {"order_id", "customer_id", "amount", "currency", "days_since_delivery", "final_sale",
                              "status"}
        assert order["amount"] == round(order["amount"], 2)
        assert 15 <= order["amount"] <= 480 or 520 <= order["amount"] <= 2400
        assert wl.is_refund_worded(t["message"]) == (cat != "not_refund")
        assert exp["expects_email"] == (cat != "partially_shipped")
        if cat == "eligible_small":
            assert order["amount"] <= 480 and 1 <= order["days_since_delivery"] <= 29 and not order["final_sale"]
            assert exp["outcome"] == "refund" and exp["amount"] == order["amount"] and not exp["requires_approval"]
        elif cat == "eligible_large":
            assert order["amount"] >= 520 and 1 <= order["days_since_delivery"] <= 29 and not order["final_sale"]
            assert exp["requires_approval"] and 3 <= exp["approval_delay_s"] <= 15
            approvals[exp["approval"]] += 1
            assert exp["outcome"] == ("refund" if exp["approval"] == "approve" else "no_refund")
        elif cat == "final_sale":
            assert order["final_sale"] and exp["outcome"] == "no_refund"
        elif cat == "outside_window":
            assert 31 <= order["days_since_delivery"] <= 90 and exp["outcome"] == "no_refund"
        elif cat == "partially_shipped":
            assert order["status"] == "partially_shipped" and order["days_since_delivery"] is None
            assert exp["outcome"] == "no_refund"
        else:
            assert cat == "not_refund" and exp["outcome"] == "no_refund"
        if not exp["requires_approval"]:
            assert exp["approval"] is None and exp["approval_delay_s"] is None
        if exp["outcome"] == "no_refund":
            assert exp["amount"] is None
    share = approvals["approve"] / sum(approvals.values())
    assert 0.7 <= share <= 0.9


def test_messages_are_varied(workload):
    messages = [t["message"] for t in workload["tickets"]]
    assert len(set(messages)) > 600
