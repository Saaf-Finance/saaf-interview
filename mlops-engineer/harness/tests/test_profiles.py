import random

import pytest

from harness.profiles import PROFILES, Phase, arrival_times, build_schedule


@pytest.mark.parametrize("profile,expected", [("smoke", 30), ("standard", 390), ("spike", 710)])
def test_arrival_counts(workload, profile, expected):
    schedule = build_schedule(profile, workload, 42)
    assert abs(len(schedule) - expected) <= 5
    duration = sum(p.duration_s for p in PROFILES[profile])
    assert all(0 <= a.at_s <= duration for a in schedule)
    assert [a.at_s for a in schedule] == sorted(a.at_s for a in schedule)
    ids = [a.ticket["ticket_id"] for a in schedule]
    assert len(set(ids)) == len(ids)


def test_standard_phases(workload):
    schedule = build_schedule("standard", workload, 42)
    by_phase = {}
    for a in schedule:
        by_phase[a.phase] = by_phase.get(a.phase, 0) + 1
    assert by_phase == {"ramp": 45, "hold": 135, "spike": 180, "tail": 30}
    spike = [a for a in schedule if a.phase == "spike"]
    assert all(75 <= a.at_s <= 90 for a in spike)
    share = sum(a.ticket["customer_id"] == "cust-01" for a in spike) / len(spike)
    assert 0.7 <= share <= 0.9


def test_non_spike_tickets_come_in_pool_order(workload):
    schedule = build_schedule("smoke", workload, 42)
    assert [a.ticket["ticket_id"] for a in schedule] == [f"T-{i:05d}" for i in range(1, 31)]


def test_ramp_rate_increases(workload):
    schedule = build_schedule("standard", workload, 42)
    first_half = sum(1 for a in schedule if a.at_s < 15)
    second_half = sum(1 for a in schedule if 15 <= a.at_s < 30)
    assert first_half < second_half
    assert abs(first_half - 11.25) <= 2 and abs(second_half - 33.75) <= 2


def test_deterministic_per_seed(workload):
    a = build_schedule("standard", workload, 42)
    b = build_schedule("standard", workload, 42)
    c = build_schedule("standard", workload, 43)
    assert [(x.at_s, x.ticket["ticket_id"]) for x in a] == [(x.at_s, x.ticket["ticket_id"]) for x in b]
    assert [x.at_s for x in a] != [x.at_s for x in c]


def test_spike_profile_survives_large_account_running_out(workload):
    schedule = build_schedule("spike", workload, 42)
    assert len(schedule) == 710
    spike = [a for a in schedule if a.phase == "spike"]
    assert sum(a.ticket["customer_id"] == "cust-01" for a in spike) / len(spike) >= 0.6


def test_phase_inverse_is_consistent():
    phase = Phase("ramp", 30, 0, 3)
    assert phase.expected_arrivals == 45
    assert phase.time_at(45) == pytest.approx(30)
    assert phase.time_at(0.05 * 10 ** 2) == pytest.approx(10)  # 0.05 t^2 arrivals by time t
    times = arrival_times([Phase("steady", 10, 2, 2)], random.Random(1))
    assert len(times) == 20
    assert all(abs(t - (i + 0.5) / 2) <= 0.2 + 1e-9 for i, (t, _) in enumerate(times))


def test_unknown_profile(workload):
    with pytest.raises(ValueError):
        build_schedule("nope", workload, 42)
