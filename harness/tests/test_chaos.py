"""Chaos against a stand-in `docker` script that only records what it was asked to do."""

import asyncio
import os
import stat
import subprocess

import pytest

from harness.chaos import Chaos, load_targets

FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "{log}"
case "$1" in
  info) {info} ;;
  compose)
    if [ "$2" = "version" ]; then echo "Docker Compose version v2"; exit 0; fi
    echo 'WARN[0000] The "TOKEN" variable is not set. Defaulting to a blank string.' >&2
    case "$4" in
      api) echo "aaaaaaaaaaaa1111"; echo "bbbbbbbbbbbb2222" ;;
      worker) echo "cccccccccccc3333" ;;
      gone) echo "dddddddddddd4444" ;;
      slow) echo "eeeeeeeeeeee5555" ;;
      noisy)
        echo "WARN[0000] Found orphan containers for this project"; echo "ffffffffffff6666"
        echo "WARN[0000] container 999999999999aaaa is restarting" >&2 ;;
      broken) echo "no such service: broken" >&2; exit 1 ;;
    esac
    ;;
  kill)
    if [ "$2" = "dddddddddddd4444" ]; then echo "Error response from daemon: container $2 is not running" >&2; exit 1; fi
    if [ "$2" = "eeeeeeeeeeee5555" ]; then exec sleep 5; fi
    echo "$2" ;;
  start) echo "$2" ;;
esac
"""


def make_docker(tmp_path, info="echo 29.0.0"):
    log = tmp_path / "calls.log"
    script = tmp_path / "docker"
    script.write_text(FAKE_DOCKER.format(log=log, info=info))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    # The first exec of a new file can take a few hundred ms (e.g. macOS scans it); pay that here, not inside the
    # short timing windows of the tests.
    subprocess.run([str(script), "warm-up"], capture_output=True, timeout=30)
    log.write_text("")
    return str(script), log


def calls(log):
    return [line.split() for line in log.read_text().splitlines()]


def test_targets_from_env_file_and_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAOS_TARGETS", raising=False)
    assert load_targets(tmp_path) == ["api"]
    (tmp_path / ".chaos").write_text("api, worker\n# a comment\nscheduler\n")
    assert load_targets(tmp_path) == ["api", "worker", "scheduler"]
    monkeypatch.setenv("CHAOS_TARGETS", "worker")
    assert load_targets(tmp_path) == ["worker"]


def test_kills_and_starts_again(tmp_path):
    docker, log = make_docker(tmp_path)
    chaos = Chaos(["api", "worker"], seed=3, repo_root=tmp_path, interval_s=(0.05, 0.1), down_s=0.05, docker=docker)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(chaos.run(stop))
        await asyncio.sleep(1.0)
        stop.set()
        await task

    asyncio.run(go())
    recorded = calls(log)
    kills = [c[1] for c in recorded if c[0] == "kill"]
    starts = [c[1] for c in recorded if c[0] == "start"]
    assert len(kills) >= 3
    assert set(kills) <= {"aaaaaaaaaaaa1111", "bbbbbbbbbbbb2222", "cccccccccccc3333"}
    for cid in kills:
        assert starts.count(cid) >= kills.count(cid)
    assert chaos.kills == len(kills)
    assert chaos._down == {}
    assert chaos.log[0]["event"] == "started" and chaos.log[-1]["event"] == "stopped"


def test_restarts_on_stop_during_the_down_window(tmp_path):
    docker, log = make_docker(tmp_path)
    chaos = Chaos(["api"], seed=1, repo_root=tmp_path, interval_s=(0.01, 0.02), down_s=30.0, docker=docker)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(chaos.run(stop))
        await asyncio.sleep(0.5)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(go())
    recorded = calls(log)
    killed = [c[1] for c in recorded if c[0] == "kill"]
    assert len(killed) == 1
    assert ["start", killed[0]] in recorded


def test_restarts_when_cancelled(tmp_path):
    docker, log = make_docker(tmp_path)
    chaos = Chaos(["api"], seed=1, repo_root=tmp_path, interval_s=(0.01, 0.02), down_s=30.0, docker=docker)

    async def go():
        task = asyncio.create_task(chaos.run(asyncio.Event()))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    recorded = calls(log)
    killed = [c[1] for c in recorded if c[0] == "kill"]
    assert len(killed) == 1 and ["start", killed[0]] in recorded
    assert any(e.get("restore") for e in chaos.log)


def run_for(chaos, seconds):
    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(chaos.run(stop))
        await asyncio.sleep(seconds)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(go())


def test_container_ids_come_from_stdout_only(tmp_path):
    docker, _ = make_docker(tmp_path)
    chaos = Chaos(["api", "noisy", "broken"], seed=1, repo_root=tmp_path, docker=docker)
    found = asyncio.run(chaos.running_containers())
    assert found == [("api", "aaaaaaaaaaaa1111"), ("api", "bbbbbbbbbbbb2222"), ("noisy", "ffffffffffff6666")]
    [failed] = [e for e in chaos.log if e["event"] == "list"]
    assert failed["service"] == "broken" and not failed["ok"]
    assert "no such service: broken" in failed["detail"]


def test_failed_kill_is_logged_and_not_started(tmp_path):
    docker, log = make_docker(tmp_path)
    chaos = Chaos(["gone"], seed=1, repo_root=tmp_path, interval_s=(0.01, 0.02), down_s=0.01, docker=docker)
    run_for(chaos, 0.5)
    recorded = calls(log)
    assert ["kill", "dddddddddddd4444"] in recorded
    assert not any(c[0] == "start" for c in recorded)
    kills = [e for e in chaos.log if e["event"] == "kill"]
    assert kills and all(not e["ok"] and "is not running" in e["detail"] for e in kills)
    assert chaos.kills == 0
    assert chaos._down == {}
    assert not any(e["event"] == "start" for e in chaos.log)


def test_timed_out_kill_is_still_started_again(tmp_path):
    docker, log = make_docker(tmp_path)
    chaos = Chaos(["slow"], seed=1, repo_root=tmp_path, interval_s=(0.01, 0.02), down_s=0.01, docker=docker)
    real_exec = chaos._exec

    async def short_kill_timeout(*args, timeout=20.0):
        return await real_exec(*args, timeout=0.2 if args[0] == "kill" else timeout)

    chaos._exec = short_kill_timeout
    run_for(chaos, 0.5)
    recorded = calls(log)
    assert ["kill", "eeeeeeeeeeee5555"] in recorded
    assert ["start", "eeeeeeeeeeee5555"] in recorded
    assert any(e["event"] == "kill" and not e["ok"] and "timed out" in e["detail"] for e in chaos.log)
    assert chaos._down == {}


def test_warns_when_docker_is_missing(tmp_path, capsys):
    chaos = Chaos(["api"], seed=1, repo_root=tmp_path, docker="no-such-docker-binary")
    asyncio.run(chaos.run(asyncio.Event()))
    assert chaos.warning and "not found" in chaos.warning
    assert "WARNING" in capsys.readouterr().err
    assert [e["event"] for e in chaos.log] == ["skipped"]


def test_warns_when_daemon_is_down(tmp_path):
    docker, log = make_docker(tmp_path, info="echo 'Cannot connect to the Docker daemon' >&2; exit 1")
    chaos = Chaos(["api"], seed=1, repo_root=tmp_path, docker=docker)
    asyncio.run(chaos.run(asyncio.Event()))
    assert "not reachable" in chaos.warning and "Cannot connect" in chaos.warning
    assert not any(c[0] == "kill" for c in calls(log))


def test_no_running_containers(tmp_path):
    docker, log = make_docker(tmp_path)
    chaos = Chaos(["nothing-here"], seed=1, repo_root=tmp_path, interval_s=(0.01, 0.02), docker=docker)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(chaos.run(stop))
        await asyncio.sleep(1.0)
        stop.set()
        await task

    asyncio.run(go())
    assert not any(c[0] == "kill" for c in calls(log))
    assert any(e["event"] == "kill" and not e["ok"] for e in chaos.log)
    assert os.path.exists(log)
