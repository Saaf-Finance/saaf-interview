"""Chaos: kill a random container of the target compose services every 10-20 s and start it again 3 s later.

Targets come from the CHAOS_TARGETS environment variable or the `.chaos` file at the repo root (comma-separated
compose service names, default "api"). Every killed container is started again: after the 3 s pause, when chaos is
stopped, and on errors or Ctrl-C (restore_all() is synchronous and idempotent). A kill that fails (say the container
exited on its own after it was listed) is logged and that container is left alone. If docker is not available, chaos
prints a warning and does nothing.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from harness import PROJECT_DIR

INTERVAL_S = (10.0, 20.0)
DOWN_S = 3.0
DEFAULT_TARGETS = ["api"]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")


def find_repo_root() -> Path:
    """The directory holding the compose file: the current directory if it has one, else the harness's parent."""
    for directory in (Path.cwd(), PROJECT_DIR.parent):
        if any((directory / name).exists() for name in COMPOSE_FILES) or (directory / ".chaos").exists():
            return directory
    return Path.cwd()


def load_targets(repo_root: Path | None = None) -> list[str]:
    raw = os.environ.get("CHAOS_TARGETS")
    if raw is None:
        path = (repo_root or find_repo_root()) / ".chaos"
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [line.split("#", 1)[0] for line in raw.splitlines()]
    targets = [t.strip() for t in ",".join(lines).split(",") if t.strip()]
    return targets or list(DEFAULT_TARGETS)


class Chaos:
    def __init__(self, targets: list[str], seed: int, *, repo_root: Path | None = None,
                 interval_s: tuple[float, float] = INTERVAL_S, down_s: float = DOWN_S, docker: str = "docker"):
        self.targets = targets
        self.rng = random.Random(seed)
        self.repo_root = repo_root or find_repo_root()
        self.interval_s = interval_s
        self.down_s = down_s
        self.docker = docker
        self.log: list[dict] = []
        self.warning: str | None = None
        self._down: dict[str, str] = {}  # container id -> service, killed and not yet started again
        self._t0 = time.time()

    @property
    def kills(self) -> int:
        return sum(1 for e in self.log if e["event"] == "kill" and e["ok"])

    def _record(self, event: str, ok: bool = True, **fields) -> None:
        self.log.append({"ts": time.time(), "t": round(time.time() - self._t0, 2), "event": event, "ok": ok, **fields})

    async def _exec(self, *args: str, timeout: float = 20.0) -> tuple[bool | None, str, str]:
        """Run docker with `args`: (ok, stdout, stderr). ok is None if it timed out, so its effect is unknown."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker, *args, cwd=self.repo_root,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            return False, "", str(exc)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return None, "", f"timed out after {timeout:.0f}s"
        return proc.returncode == 0, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()

    async def check_docker(self) -> str | None:
        """None if docker and compose work, else the reason they don't."""
        if shutil.which(self.docker) is None:
            return f"'{self.docker}' was not found on PATH"
        ok, out, err = await self._exec("info", "--format", "{{.ServerVersion}}")
        if not ok:
            return f"the docker daemon is not reachable ({_last_line(out, err)})"
        ok, out, err = await self._exec("compose", "version")
        if not ok:
            return f"'docker compose' is not available ({_last_line(out, err)})"
        return None

    async def running_containers(self) -> list[tuple[str, str]]:
        found = []
        for service in self.targets:
            ok, out, err = await self._exec("compose", "ps", "-q", service)
            if not ok:
                self._record("list", ok=False, service=service, detail=_detail(out, err))
                continue
            # Only stdout holds ids; compose writes warnings (e.g. unset variables) to stderr.
            found.extend((service, cid) for cid in out.split() if CONTAINER_ID.fullmatch(cid))
        return sorted(found)

    async def run(self, stop: asyncio.Event) -> None:
        """Kill and restart containers until `stop` is set. Always leaves every container it killed started."""
        self._t0 = time.time()
        reason = await self.check_docker()
        if reason:
            self.warning = f"chaos is on but docker is unavailable: {reason}. Running without chaos."
            print(f"WARNING: {self.warning}", file=sys.stderr, flush=True)
            self._record("skipped", ok=False, detail=reason)
            return
        self._record("started", targets=self.targets)
        try:
            while not await _wait(stop, self.rng.uniform(*self.interval_s)):
                containers = await self.running_containers()
                if not containers:
                    self._record("kill", ok=False, detail=f"no running containers for {','.join(self.targets)}")
                    continue
                service, cid = self.rng.choice(containers)
                self._down[cid] = service  # noted before the kill, so an interrupted kill is still undone
                ok, out, err = await self._exec("kill", cid)
                self._record("kill", ok=bool(ok), service=service, container=cid[:12], detail=_detail(out, err))
                if ok is False:
                    # Nothing was killed (e.g. the container already exited), so there is nothing to start again.
                    # A timed-out kill (ok is None) may still have gone through and is started again below.
                    self._down.pop(cid, None)
                    continue
                await _wait(stop, self.down_s)
                await self._start(cid)
        finally:
            self.restore_all()
            self._record("stopped")

    async def _start(self, cid: str) -> None:
        ok, out, err = await self._exec("start", cid)
        self._record("start", ok=bool(ok), service=self._down.get(cid), container=cid[:12], detail=_detail(out, err))
        if ok:
            self._down.pop(cid, None)

    def restore_all(self) -> None:
        """Start every container this instance killed and has not started yet (blocking, safe to call twice)."""
        for cid, service in list(self._down.items()):
            for _ in range(3):
                try:
                    proc = subprocess.run([self.docker, "start", cid], cwd=self.repo_root, capture_output=True,
                                          text=True, timeout=30)
                    ok, detail = proc.returncode == 0, (proc.stdout + proc.stderr).strip()
                except (OSError, subprocess.TimeoutExpired) as exc:
                    ok, detail = False, str(exc)
                self._record("start", ok=ok, service=service, container=cid[:12], detail=detail[-300:],
                             restore=True)
                if ok:
                    self._down.pop(cid, None)
                    break
            else:
                print(f"WARNING: could not start container {cid[:12]} ({service}) again; "
                      f"run `docker start {cid[:12]}` or `make up`.", file=sys.stderr, flush=True)


def _detail(out: str, err: str) -> str:
    """The tail of a command's stdout and stderr, for the chaos log."""
    return "\n".join(s for s in (out, err) if s)[-300:]


def _last_line(out: str, err: str) -> str:
    lines = _detail(out, err).splitlines()
    return lines[-1] if lines else "no output"


async def _wait(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to `seconds`; True if `stop` was set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return stop.is_set()
