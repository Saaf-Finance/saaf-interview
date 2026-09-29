"""Run the benchmark against the agent service and print the scoreboard.

    uv run --project harness python -m harness.bench --profile standard

Flow: reset both ledgers -> wait for the service's /healthz -> run load, approver and chaos together -> after the last
submit keep approving (and chaos) while waiting for every ticket to reach a finished run -> stop chaos (every killed
container is started again) -> verify -> print the scoreboard -> write results/<UTC timestamp>.json and
results/latest.md.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import platform
import signal
import sys
import time
from datetime import datetime, timezone

import httpx

from harness import DEFAULT_WORKLOAD_PATH
from harness import workload as workload_mod
from harness.approver import Approver
from harness.chaos import Chaos, find_repo_root, load_targets
from harness.load import LoadRunner
from harness.profiles import PROFILES, build_schedule
from harness.report import render_scoreboard, write_results
from harness.verify import TERMINAL_STATUSES, compute, fetch_runs, observe

HEALTH_TIMEOUT_S = 60.0
RESET_TIMEOUT_S = 30.0
DRAIN_POLL_S = 2.0
PROGRESS_EVERY_S = 5.0

_active_chaos: list[Chaos] = []
_t0 = time.monotonic()


class BenchError(Exception):
    """The bench could not run (a service is unreachable)."""


def log(message: str) -> None:
    print(f"[{time.monotonic() - _t0:6.1f}s] {message}", file=sys.stderr, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m harness.bench", description="Benchmark the agent service.")
    p.add_argument("--profile", choices=list(PROFILES), default="standard")
    p.add_argument("--chaos", action=argparse.BooleanOptionalAction, default=None,
                   help="kill and restart service containers during the run (default: on, except for smoke)")
    p.add_argument("--seed", type=int, default=42, help="seeds arrival jitter, spike ticket choice and chaos timing")
    p.add_argument("--duplicate-rate", type=float, default=0.05,
                   help="share of tickets delivered twice with the same Idempotency-Key (at-least-once delivery)")
    p.add_argument("--sut", default="http://localhost:8000", help="the agent service")
    p.add_argument("--llm", default="http://localhost:8100", help="the fake LLM provider")
    p.add_argument("--commerce", default="http://localhost:8200", help="the store backend")
    p.add_argument("--drain-timeout", type=float, default=180.0,
                   help="seconds to wait after the last submit for every ticket to finish")
    p.add_argument("--out", default="results", help="directory for the JSON result and latest.md")
    p.add_argument("--workload", default=str(DEFAULT_WORKLOAD_PATH), help=argparse.SUPPRESS)
    return p.parse_args(argv)


async def reset_ledgers(client: httpx.AsyncClient, llm_url: str, commerce_url: str) -> None:
    for name, url in (("LLM provider", llm_url), ("store backend", commerce_url)):
        deadline = time.monotonic() + RESET_TIMEOUT_S
        error = ""
        while True:
            try:
                resp = await client.post(f"{url}/admin/reset")
                if 200 <= resp.status_code < 300:
                    break
                error = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() > deadline:
                raise BenchError(f"could not reset the {name} ledger at {url} ({error}). Is `make up` running?")
            await asyncio.sleep(1.0)


async def wait_healthy(client: httpx.AsyncClient, url: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if (await client.get(f"{url}/healthz", timeout=5.0)).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(1.0)


async def tickets_with_runs(client: httpx.AsyncClient, sut_url: str, ticket_ids: list[str]) -> list[str]:
    """Tickets the service already has runs for (left over from an earlier bench against the same service)."""
    semaphore = asyncio.Semaphore(16)

    async def has_runs(ticket_id: str) -> bool:
        async with semaphore:
            runs, _ = await fetch_runs(client, sut_url, ticket_id, attempts=1)
        return bool(runs)

    found = await asyncio.gather(*(has_runs(t) for t in ticket_ids))
    return [t for t, yes in zip(ticket_ids, found) if yes]


async def drain(client: httpx.AsyncClient, sut_url: str, ticket_ids: list[str], timeout_s: float,
                approver: Approver) -> dict:
    """Poll until every ticket has a finished run (completed or failed) or the timeout passes."""
    start = time.monotonic()
    pending = set(ticket_ids)
    semaphore = asyncio.Semaphore(16)
    last_log = 0.0

    async def finished(ticket_id: str) -> bool:
        async with semaphore:
            runs, _ = await fetch_runs(client, sut_url, ticket_id, attempts=1)
        return any(isinstance(r.get("status"), str) and r["status"].lower() in TERMINAL_STATUSES for r in runs)

    while pending:
        order = sorted(pending)
        done = await asyncio.gather(*(finished(t) for t in order))
        pending -= {t for t, ok in zip(order, done) if ok}
        elapsed = time.monotonic() - start
        if not pending or elapsed >= timeout_s:
            break
        if elapsed - last_log >= 10.0:
            last_log = elapsed
            log(f"drain: {len(pending)} tickets without a finished run, {approver.pending} approvals pending")
        await asyncio.sleep(min(DRAIN_POLL_S, max(0.0, timeout_s - elapsed)))
    return {
        "complete": not pending,
        "duration_s": round(time.monotonic() - start, 2),
        "unfinished": len(pending),
        "unfinished_tickets": sorted(pending),
    }


async def _progress(runner: LoadRunner, approver: Approver, chaos: Chaos | None, total: int) -> None:
    while True:
        await asyncio.sleep(PROGRESS_EVERY_S)
        accepted = sum(1 for r in runner.results.values() if r.accepted)
        chaos_text = f", chaos kills {chaos.kills}" if chaos else ""
        log(f"submitted {runner.started}/{total} (accepted {accepted}, in flight {runner.started - runner.finished})"
            f", approvals pending {approver.pending}{chaos_text}")


async def run_bench(args: argparse.Namespace) -> dict:
    workload = workload_mod.load(args.workload)
    schedule = build_schedule(args.profile, workload, args.seed)
    chaos_on = args.chaos if args.chaos is not None else args.profile != "smoke"
    sut, llm, commerce = args.sut.rstrip("/"), args.llm.rstrip("/"), args.commerce.rstrip("/")
    warnings: list[str] = []

    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        log(f"resetting ledgers at {llm} and {commerce}")
        await reset_ledgers(client, llm, commerce)
        log(f"waiting for {sut}/healthz")
        if not await wait_healthy(client, sut, HEALTH_TIMEOUT_S):
            raise BenchError(f"{sut}/healthz did not return 200 within {HEALTH_TIMEOUT_S:.0f}s. Is `make up` running?")
        ticket_ids = [a.ticket["ticket_id"] for a in schedule]
        preexisting = await tickets_with_runs(client, sut, ticket_ids)
        if preexisting:
            message = (f"the service already had runs for {len(preexisting)} of this profile's tickets before the "
                       f"bench started (e.g. {', '.join(preexisting[:3])}), probably from an earlier bench; the ledgers "
                       f"were reset but the service was not, so results for those tickets are unreliable. Restart the "
                       f"service between benches (e.g. `make restart`).")
            log(f"WARNING: {message}")
            warnings.append(message)

        started = time.time()
        log(f"profile {args.profile}: {len(schedule)} tickets over {schedule[-1].at_s:.0f}s, "
            f"chaos {'on' if chaos_on else 'off'}")
        approver = Approver(sut, workload["expected"])
        runner = LoadRunner(sut, duplicate_rate=args.duplicate_rate, seed=args.seed)
        chaos = Chaos(load_targets(find_repo_root()), args.seed) if chaos_on else None
        stop_approver, stop_chaos = asyncio.Event(), asyncio.Event()
        approver_task = asyncio.create_task(approver.run(stop_approver))
        chaos_task = None
        if chaos is not None:
            _active_chaos.append(chaos)
            chaos_task = asyncio.create_task(chaos.run(stop_chaos))
        progress_task = asyncio.create_task(_progress(runner, approver, chaos, len(schedule)))
        load_s = 0.0
        try:
            await runner.run(schedule)
            load_s = time.time() - started
            log(f"load finished after {load_s:.1f}s; draining (timeout {args.drain_timeout:.0f}s)")
            drain_info = await drain(client, sut, ticket_ids, args.drain_timeout, approver)
        finally:
            progress_task.cancel()
            stop_chaos.set()
            if chaos_task is not None:
                await asyncio.gather(chaos_task, return_exceptions=True)
                chaos.restore_all()
            stop_approver.set()
            try:
                await asyncio.wait_for(asyncio.shield(approver_task), timeout=8.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                approver_task.cancel()

        if not drain_info["complete"]:
            warnings.append(f"drain timed out after {args.drain_timeout:.0f}s: "
                            f"{drain_info['unfinished']} tickets had no finished run")
        if chaos is not None and chaos.warning:
            warnings.append(chaos.warning)
        if chaos is not None and chaos.kills:
            log("chaos stopped; waiting for the service to be healthy before verifying")
            if not await wait_healthy(client, sut, HEALTH_TIMEOUT_S):
                warnings.append(f"{sut}/healthz was not healthy {HEALTH_TIMEOUT_S:.0f}s after chaos stopped")

    log("verifying")
    submits = [r.to_dict() for r in sorted(runner.results.values(), key=lambda r: r.scheduled_at)]
    observed = await observe(sut, llm, commerce, [s["ticket_id"] for s in submits])
    approvals = approver.per_ticket()
    metrics = compute(submits, workload, observed, approvals)
    if observed["run_errors"]:
        warnings.append(f"{len(observed['run_errors'])} run lookups failed during verification "
                        f"(those tickets count as lost)")
    if observed["commerce_error"]:
        warnings.append(f"store ledger unavailable: {observed['commerce_error']}")
    if observed["llm_error"]:
        warnings.append(f"LLM ledger unavailable: {observed['llm_error']}")

    return {
        "meta": {
            "profile": args.profile,
            "seed": args.seed,
            "sut": sut,
            "llm": llm,
            "commerce": commerce,
            "workload": str(args.workload),
            "workload_seed": workload.get("seed"),
            "tickets": len(schedule),
            "started_at_utc": datetime.fromtimestamp(started, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "started_ts": started,
            "finished_ts": time.time(),
            "load_s": round(load_s, 2),
            "drain_timeout_s": args.drain_timeout,
            "drain": drain_info,
            "preexisting_run_tickets": preexisting,
            "approver": {"polls": approver.polls, "poll_errors": approver.poll_errors},
            "python": platform.python_version(),
        },
        "chaos": {
            "enabled": chaos_on,
            "targets": chaos.targets if chaos else [],
            "kills": chaos.kills if chaos else 0,
            "warning": chaos.warning if chaos else None,
            "log": chaos.log if chaos else [],
        },
        "warnings": warnings,
        "metrics": metrics,
        "ledgers": {
            "llm": {k: v for k, v in (observed["llm"] or {}).items() if k != "per_ticket"},
            "store": {k: v for k, v in (observed["commerce"] or {}).items() if k not in ("refunds", "emails")},
            "refunds": (observed["commerce"] or {}).get("refunds"),
            "emails": (observed["commerce"] or {}).get("emails"),
        },
    }


def _restore_chaos() -> None:
    for chaos in _active_chaos:
        chaos.restore_all()


def _raise_open_file_limit(target: int = 8192) -> None:
    """Many concurrent submits need many sockets; lift a low soft limit where the OS allows it."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ImportError, ValueError, OSError):
        pass


def _on_sigterm(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    global _t0
    args = parse_args(argv)
    _t0 = time.monotonic()
    _raise_open_file_limit()
    atexit.register(_restore_chaos)
    previous_handler = None
    try:
        previous_handler = signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # not in the main thread
        pass
    try:
        result = asyncio.run(run_bench(args))
    except KeyboardInterrupt:
        _restore_chaos()
        print("\ninterrupted; any containers stopped by chaos were started again", file=sys.stderr)
        return 130
    except BenchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        _restore_chaos()
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)
    json_path, md_path = write_results(result, args.out)
    print(render_scoreboard(result))
    print(f"\nwrote {json_path} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
