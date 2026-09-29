"""Chaos against a stand-in `docker` script that only records what it was asked to do."""

import asyncio
import os
import stat

import pytest

from harness.chaos import Chaos, load_targets

FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "{log}"
case "$1" in
  info) {info} ;;
  compose)
    if [ "$2" = "version" ]; then echo "Docker Compose version v2"; exit 0; fi
    if [ "$4" = "api" ]; then echo "aaaaaaaaaaaa1111"; echo "bbbbbbbbbbbb2222"; fi
    if [ "$4" = "worker" ]; then echo "cccccccccccc3333"; fi
    ;;
  kill|start) echo "$2" ;;
esac
"""


def make_docker(tmp_path, info="echo 29.0.0"):
    log = tmp_path / "calls.log"
    log.touch()
    script = tmp_path / "docker"
    script.write_text(FAKE_DOCKER.format(log=log, info=info))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
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
