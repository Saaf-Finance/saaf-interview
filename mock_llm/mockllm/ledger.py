"""In-memory usage ledger: one entry per chat completion request, whatever its outcome."""

from __future__ import annotations

from collections import Counter
from typing import Any


class Ledger:
    def __init__(self, price_input_per_mtok: float, price_output_per_mtok: float) -> None:
        self.price_input_per_mtok = price_input_per_mtok
        self.price_output_per_mtok = price_output_per_mtok
        self.entries: list[dict[str, Any]] = []

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.price_input_per_mtok
                + completion_tokens * self.price_output_per_mtok) / 1_000_000

    def record(self, *, ts: float, ticket_id: str | None, run_id: str | None, task: str | None,
               status_code: int, prompt_tokens: int, completion_tokens: int = 0,
               latency_s: float = 0.0, stream: bool = False, dropped: bool = False) -> dict[str, Any]:
        """Append an entry and return it. Only successful (200) calls are billed."""
        entry = {
            "ts": ts,
            "ticket_id": ticket_id,
            "run_id": run_id,
            "task": task,
            "status_code": status_code,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": 0.0,
            "latency_s": round(latency_s, 4),
            "stream": stream,
            "dropped": dropped,
        }
        self.bill(entry)
        self.entries.append(entry)
        return entry

    def bill(self, entry: dict[str, Any]) -> None:
        """(Re)compute an entry's cost from its status and token counts."""
        billed = entry["status_code"] == 200
        entry["cost_usd"] = (
            round(self.cost(entry["prompt_tokens"], entry["completion_tokens"]), 8) if billed else 0.0
        )

    def clear(self) -> None:
        self.entries.clear()

    def summary(self) -> dict[str, Any]:
        """Totals over all entries, plus a per-ticket breakdown (calls with an X-Ticket-Id)."""
        by_status = Counter(str(e["status_code"]) for e in self.entries)
        per_ticket: dict[str, dict[str, Any]] = {}
        for e in self.entries:
            if not e["ticket_id"]:
                continue
            t = per_ticket.setdefault(
                e["ticket_id"], {"calls": 0, "ok_calls": 0, "cost_usd": 0.0, "tokens": 0})
            t["calls"] += 1
            t["ok_calls"] += e["status_code"] == 200
            t["cost_usd"] += e["cost_usd"]
            t["tokens"] += e["prompt_tokens"] + e["completion_tokens"]
        for t in per_ticket.values():
            t["cost_usd"] = round(t["cost_usd"], 6)
        return {
            "calls": len(self.entries),
            "by_status": dict(sorted(by_status.items())),
            "prompt_tokens": sum(e["prompt_tokens"] for e in self.entries),
            "completion_tokens": sum(e["completion_tokens"] for e in self.entries),
            "cost_usd": round(sum(e["cost_usd"] for e in self.entries), 6),
            "dropped_streams": sum(1 for e in self.entries if e["dropped"]),
            "per_ticket": per_ticket,
        }
