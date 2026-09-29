"""End-to-end: the bench CLI against the in-process fakes."""

import json
import stat

from harness import bench, profiles
from harness.profiles import Phase


def args_for(servers, out, *extra):
    sut, llm, commerce = (s.url for s in servers)
    return ["--sut", sut, "--llm", llm, "--commerce", commerce, "--out", str(out), *extra]


def test_smoke_profile_against_a_correct_service(services, tmp_path, capsys):
    sut, llm, commerce, servers = services()
    code = bench.main(args_for(servers, tmp_path, "--profile", "smoke", "--no-chaos", "--drain-timeout", "60"))
    assert code == 0
    out = capsys.readouterr().out
    assert "HARD TARGETS: PASS" in out
    for label in ("lost runs", "duplicate refunds", "unexpected refunds", "missing refunds", "wrong-amount refunds",
                  "refunds before approval", "LLM 429 rate", "p95 time to finish, automated tickets"):
        assert label in out
    lines = [line for line in out.splitlines() if line.startswith("  lost runs")]
    assert lines and lines[0].split()[-1] == "PASS"

    result_files = list(tmp_path.glob("*.json"))
    assert len(result_files) == 1
    result = json.loads(result_files[0].read_text())
    assert (tmp_path / "latest.md").read_text().count("HARD TARGETS: PASS") == 1
    metrics = result["metrics"]
    assert metrics["hard"] == dict.fromkeys(metrics["hard"], 0)
    assert result["meta"]["tickets"] == 30 and result["meta"]["drain"]["complete"]
    assert result["chaos"] == {"enabled": False, "targets": [], "kills": 0, "warning": None, "log": []}
    assert metrics["soft"]["submits"] == {"tickets": 30, "attempts": 30, "failed_tickets": 0}
    assert metrics["info"]["approvals"]["tickets_requiring_approval"] == 1
    assert metrics["info"]["approvals"]["seen_waiting"] == 1
    assert metrics["soft"]["latency_automated_s"]["n"] == 29
    assert len(metrics["tickets"]) == 30 and metrics["soft"]["llm"]["calls"] > 30
    assert len(commerce.emails) == 29  # every ticket except the partially shipped one


def test_faults_are_detected_end_to_end(services, workload, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(profiles.PROFILES, "smoke", [Phase("steady", 4, 10, 10)])  # T-00001..T-00040 in 4 s
    fast = json.loads(json.dumps(workload))
    fast["expected"]["T-00027"]["approval_delay_s"] = 0.2
    workload_path = tmp_path / "workload.json"
    workload_path.write_text(json.dumps(fast))

    # A docker that is installed but whose daemon is down: chaos must only warn.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\necho 'Cannot connect to the Docker daemon' >&2\nexit 1\n")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    faults = {
        "duplicate_refund": {"T-00001"},
        "lose": {"T-00003"},
        "duplicate_email": {"T-00002"},
        "malformed_runs": {"T-00004"},
        "skip_approval": True,  # T-00027 (a rejected large refund) gets refunded without asking
    }
    sut, llm, commerce, servers = services(faults=faults)
    out_dir = tmp_path / "results"
    code = bench.main(args_for(servers, out_dir, "--profile", "smoke", "--chaos", "--drain-timeout", "4",
                               "--workload", str(workload_path)))
    assert code == 0
    out = capsys.readouterr().out
    assert "HARD TARGETS: FAIL" in out
    assert "docker is unavailable" in out

    result = json.loads(next(out_dir.glob("*.json")).read_text())
    hard = result["metrics"]["hard"]
    assert hard == {"lost_runs": 2, "duplicate_runs": 0, "duplicate_refunds": 1, "unexpected_refunds": 1, "missing_refunds": 0,
                    "wrong_amount_refunds": 0, "refund_before_approval": 1}
    assert result["metrics"]["soft"]["duplicate_emails"] == 1
    flagged = result["metrics"]["flagged"]
    assert sorted(flagged["lost_runs"]) == ["T-00003", "T-00004"]
    assert flagged["refund_before_approval"] == ["T-00027"]
    assert result["chaos"]["enabled"] and "Cannot connect" in result["chaos"]["warning"]
    assert result["meta"]["drain"]["complete"] is False
    assert any("run lookups failed" in w for w in result["warnings"])
    md = (out_dir / "latest.md").read_text()
    assert "lost_runs" in md and "T-00003" in md


def test_warns_about_runs_left_from_an_earlier_bench(services, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(profiles.PROFILES, "smoke", [Phase("steady", 1, 10, 10)])  # T-00001..T-00010
    sut, llm, commerce, servers = services()
    first, second = tmp_path / "first", tmp_path / "second"
    assert bench.main(args_for(servers, first, "--profile", "smoke", "--no-chaos")) == 0
    assert bench.main(args_for(servers, second, "--profile", "smoke", "--no-chaos")) == 0
    before = json.loads(next(first.glob("*.json")).read_text())
    after = json.loads(next(second.glob("*.json")).read_text())
    assert before["warnings"] == [] and before["meta"]["preexisting_run_tickets"] == []
    assert len(after["meta"]["preexisting_run_tickets"]) == 10
    assert any("already had runs for 10" in w for w in after["warnings"])
    assert "make restart" in capsys.readouterr().out


def test_unreachable_service_exits_with_error(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(bench, "RESET_TIMEOUT_S", 0.5)
    code = bench.main(["--sut", "http://127.0.0.1:9", "--llm", "http://127.0.0.1:9", "--commerce",
                       "http://127.0.0.1:9", "--profile", "smoke", "--out", str(tmp_path)])
    assert code == 2
    assert "Is `make up` running?" in capsys.readouterr().err
    assert not list(tmp_path.glob("*.json"))
