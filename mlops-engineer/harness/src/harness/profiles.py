"""Traffic profiles: when each ticket arrives and which ticket it is.

A profile is a list of phases with a linear arrival rate (tickets/second). Arrivals are open-loop: they are placed
where the cumulative expected count crosses k + 0.5, plus a small seeded jitter, so a profile always produces the same
number of tickets. Tickets are taken from the pool in order; during a spike 80% of arrivals come from the large account.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

SPIKE_LARGE_ACCOUNT_SHARE = 0.8
JITTER = 0.4  # in units of "one arrival", so arrivals never swap order


@dataclass(frozen=True)
class Phase:
    name: str
    duration_s: float
    start_rate: float
    end_rate: float
    spike: bool = False

    @property
    def expected_arrivals(self) -> float:
        return (self.start_rate + self.end_rate) / 2 * self.duration_s

    def time_at(self, count: float) -> float:
        """Seconds into the phase at which `count` arrivals are expected (inverse of the cumulative rate)."""
        a = (self.end_rate - self.start_rate) / (2 * self.duration_s)
        b = self.start_rate
        if abs(a) < 1e-12:
            t = count / b
        else:
            t = (-b + math.sqrt(max(0.0, b * b + 4 * a * count))) / (2 * a)
        return min(max(t, 0.0), self.duration_s)


PROFILES: dict[str, list[Phase]] = {
    "smoke": [Phase("steady", 30, 1, 1)],
    "standard": [
        Phase("ramp", 30, 0, 3),
        Phase("hold", 45, 3, 3),
        Phase("spike", 15, 12, 12, spike=True),
        Phase("tail", 30, 1, 1),
    ],
    "spike": [
        Phase("ramp", 30, 0, 3),
        Phase("hold", 45, 3, 3),
        Phase("spike", 20, 25, 25, spike=True),
        Phase("tail", 30, 1, 1),
    ],
}


@dataclass(frozen=True)
class Arrival:
    at_s: float  # seconds after the load starts
    phase: str
    ticket: dict


def arrival_times(phases: list[Phase], rng: random.Random) -> list[tuple[float, Phase]]:
    """Arrival offsets (seconds from start) for a list of phases."""
    out: list[tuple[float, Phase]] = []
    offset = 0.0
    for phase in phases:
        n = round(phase.expected_arrivals)
        for k in range(n):
            count = (k + 0.5 + rng.uniform(-JITTER, JITTER)) * phase.expected_arrivals / n
            out.append((offset + phase.time_at(count), phase))
        offset += phase.duration_s
    return out


class _TicketPicker:
    """Hands out pool tickets in order, optionally restricted to (or excluding) one customer."""

    def __init__(self, tickets: list[dict], large_account: str):
        self._tickets = tickets
        self._large = large_account
        self._used: set[str] = set()
        self._cursor = {"any": 0, "large": 0, "other": 0}

    def _match(self, kind: str, ticket: dict) -> bool:
        if kind == "large":
            return ticket["customer_id"] == self._large
        if kind == "other":
            return ticket["customer_id"] != self._large
        return True

    def next(self, kind: str = "any") -> dict:
        i = self._cursor[kind]
        while i < len(self._tickets):
            ticket = self._tickets[i]
            i += 1
            if ticket["ticket_id"] not in self._used and self._match(kind, ticket):
                self._cursor[kind] = i
                self._used.add(ticket["ticket_id"])
                return ticket
        self._cursor[kind] = i
        if kind != "any":
            return self.next("any")  # that customer group ran out; fall back to the pool order
        raise ValueError("the workload pool has no tickets left for this profile")


def large_account_of(workload: dict) -> str:
    for customer in workload.get("customers", []):
        if customer.get("segment") == "large":
            return customer["customer_id"]
    return "cust-01"


def build_schedule(profile: str, workload: dict, seed: int) -> list[Arrival]:
    """The full, deterministic arrival schedule for a profile."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; choose one of {', '.join(PROFILES)}")
    rng = random.Random(seed)
    picker = _TicketPicker(workload["tickets"], large_account_of(workload))
    schedule = []
    for at_s, phase in arrival_times(PROFILES[profile], rng):
        kind = "any"
        if phase.spike:
            kind = "large" if rng.random() < SPIKE_LARGE_ACCOUNT_SHARE else "other"
        schedule.append(Arrival(at_s=round(at_s, 3), phase=phase.name, ticket=picker.next(kind)))
    return schedule
