"""Render the scoreboard (plain text), results/latest.md and the full JSON result."""

from __future__ import annotations

import json
from pathlib import Path


def _n(value) -> str:
    return "n/a" if value is None else str(value)


def _s(value) -> str:
    return "n/a" if value is None else f"{value:.1f}s"


def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _usd(value) -> str:
    return "n/a" if value is None else f"${value:.4f}"


HARD_LABELS = {
    "lost_runs": "lost runs",
    "duplicate_runs": "duplicate runs (tickets with >1 run)",
    "duplicate_refunds": "duplicate refunds",
    "unexpected_refunds": "unexpected refunds",
    "missing_refunds": "missing refunds",
    "wrong_amount_refunds": "wrong-amount refunds",
    "refund_before_approval": "refunds before approval",
}


def _rows(result: dict) -> list:
    m = result["metrics"]
    hard, soft, info, checks = m["hard"], m["soft"], m["info"], m["checks"]
    lat, llm, cost, submits = soft["latency_automated_s"], soft["llm"], soft["cost"], soft["submits"]
    targets = m["targets"]["soft"]

    rows: list = ["Hard targets"]
    for name, label in HARD_LABELS.items():
        rows.append((label, _n(hard[name]), "= 0", checks["hard"][name]))

    rows.append("Soft targets")
    rows.append(("p95 time to finish, automated tickets", _s(lat["p95"]),
                 f"< {targets['latency_p95_automated_s']:.0f}s", checks["soft"]["latency_p95_automated_s"]))
    rows.append(("LLM 429 rate", _pct(llm.get("rate_429")), f"< {targets['llm_429_rate'] * 100:.0f}%",
                 checks["soft"]["llm_429_rate"]))

    worst = soft["worst_small_customer_p95"]
    large = soft["large_account_p95"]
    rows.append("Other metrics (lower is better)")
    rows += [
        ("duplicate emails", _n(soft["duplicate_emails"]), "-", ""),
        ("missing emails", _n(soft["missing_emails"]), "-", ""),
        (f"p50 / p99 time to finish, automated (n={lat['n']})", f"{_s(lat['p50'])} / {_s(lat['p99'])}", "-", ""),
        ("worst small-customer p95",
         f"{_s(worst['p95_s'])} ({worst['customer_id']})" if worst else "n/a", "-", ""),
    ]
    for customer, value in large.items():
        rows.append((f"large-account p95 ({customer})", _s(value["p95_s"]), "-", ""))
    if llm.get("available"):
        rows += [
            ("LLM calls / 429s / 5xx", f"{llm['calls']} / {llm['calls_429']} / {llm['calls_5xx']}", "-", ""),
            ("LLM calls per ticket", f"{llm['calls_per_ticket_mean']:.1f}" if llm["calls_per_ticket_mean"] else "n/a",
             "-", ""),
            ("LLM calls without X-Ticket-Id", _n(llm["unattributed_calls"]), "-", ""),
            ("dropped streams", _n(llm["dropped_streams"]), "-", ""),
            ("LLM cost, total", _usd(cost["total_usd"]), "-", ""),
            ("cost per ticket p50 / p95 / max",
             f"{_usd(cost['per_ticket_p50_usd'])} / {_usd(cost['per_ticket_p95_usd'])} / "
             f"{_usd(cost['per_ticket_max_usd'])}", "-", ""),
            ("mean cost: partially shipped vs other tickets",
             f"{_usd(cost['partially_shipped_mean_usd'])} vs {_usd(cost['others_mean_usd'])}", "-", ""),
        ]
    else:
        rows.append(("LLM usage and cost", "n/a (ledger unavailable)", "-", ""))
    approvals = info["approvals"]
    rows += [
        ("submit attempts / tickets never accepted", f"{submits['attempts']} / {submits['failed_tickets']}", "-", ""),
        ("approval tickets seen waiting / expected",
         f"{approvals['seen_waiting']} / {approvals['tickets_requiring_approval']}", "-", ""),
        ("refunds / emails recorded", f"{_n(info['refunds_total'])} / {_n(info['emails_total'])}", "-", ""),
    ]
    return rows


def render_scoreboard(result: dict) -> str:
    meta, m = result["meta"], result["metrics"]
    rows = _rows(result)
    header = ("METRIC", "VALUE", "TARGET", "RESULT")
    table = [r for r in rows if isinstance(r, tuple)] + [header]
    w = [max(len(r[i]) for r in table) for i in range(4)]
    width = 2 + w[0] + 3 + w[1] + 3 + w[2] + 3 + w[3]

    def line(r: tuple) -> str:
        return f"  {r[0]:<{w[0]}}   {r[1]:>{w[1]}}   {r[2]:>{w[2]}}   {r[3]:>{w[3]}}".rstrip()

    chaos = result["chaos"]
    chaos_text = f"on, {chaos['kills']} kills" if chaos["enabled"] and not chaos.get("warning") else (
        "on, but docker unavailable" if chaos["enabled"] else "off")
    drain = meta["drain"]
    drain_text = "all runs finished" if drain["complete"] else f"timed out, {drain['unfinished']} unfinished"
    out = [
        "=" * width,
        f"BENCH  profile={meta['profile']}  seed={meta['seed']}  chaos={chaos_text}  tickets={meta['tickets']}",
        f"sut={meta['sut']}  started={meta['started_at_utc']}",
        f"load {meta['load_s']:.1f}s  drain {drain['duration_s']:.1f}s ({drain_text})",
        "=" * width,
        line(header),
    ]
    for r in rows:
        if isinstance(r, str):
            out += ["-" * width, r]
        else:
            out.append(line(r))
    passed = sum(1 for v in m["checks"]["hard"].values() if v == "PASS")
    total = len(m["checks"]["hard"])
    soft_met = sum(1 for v in m["checks"]["soft"].values() if v == "met")
    out += [
        "=" * width,
        f"HARD TARGETS: {'PASS' if m['passed'] else 'FAIL'} ({passed}/{total} at 0)    "
        f"SOFT TARGETS: {soft_met}/{len(m['checks']['soft'])} met",
    ]
    for warning in result.get("warnings", []):
        out.append(f"! {warning}")
    return "\n".join(out)


def render_markdown(result: dict, json_name: str) -> str:
    meta, m = result["meta"], result["metrics"]
    parts = [
        f"# Bench result: {meta['profile']} ({meta['started_at_utc']})",
        "",
        f"Full details: `{json_name}`",
        "",
        "```",
        render_scoreboard(result),
        "```",
        "",
        "## Flagged tickets",
        "",
    ]
    if m["flagged"]:
        for flag, tickets in sorted(m["flagged"].items()):
            more = f" … and {len(tickets) - 20} more" if len(tickets) > 20 else ""
            parts.append(f"- **{flag}** ({len(tickets)}): {', '.join(tickets[:20])}{more}")
    else:
        parts.append("None.")
    chaos = result["chaos"]
    parts += ["", "## Chaos", ""]
    if not chaos["enabled"]:
        parts.append("Chaos was off.")
    elif chaos.get("warning"):
        parts.append(chaos["warning"])
    else:
        parts.append(f"Targets: {', '.join(chaos['targets'])}")
        parts += ["", "| t (s) | event | service | container | ok | detail |", "|---|---|---|---|---|---|"]
        for e in chaos["log"]:
            detail = str(e.get("detail", "")).replace("|", "/").replace("\n", " ")[:80]
            parts.append(f"| {e['t']} | {e['event']} | {e.get('service', '')} | {e.get('container', '')} | "
                         f"{'yes' if e['ok'] else 'no'} | {detail} |")
    return "\n".join(parts) + "\n"


def write_results(result: dict, out_dir: str | Path) -> tuple[Path, Path]:
    """Write <out>/<UTC timestamp>.json and <out>/latest.md; returns both paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = result["meta"]["started_at_utc"].replace("-", "").replace(":", "")
    json_path = out / f"{stamp}.json"
    n = 1
    while json_path.exists():
        n += 1
        json_path = out / f"{stamp}-{n}.json"
    json_path.write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8")
    md_path = out / "latest.md"
    md_path.write_text(render_markdown(result, json_path.name), encoding="utf-8")
    return json_path, md_path
