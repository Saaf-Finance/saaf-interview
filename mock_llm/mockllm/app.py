"""Mock LLM provider: an OpenAI-compatible subset of POST /v1/chat/completions.

Run as a single process: rate limits, the usage ledger and the config all live in memory.

Request pipeline:
    1. estimate tokens      2. rate limit (429)       3. random server errors (500/503)
    4. simulated latency    5. build the answer       6. JSON or SSE response
    7. every request, whatever the outcome, is written to the ledger
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import random
import time
import uuid
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from mockllm.ledger import Ledger
from mockllm.limiter import Decision, RateLimiter
from mockllm.settings import Settings
from mockllm.tasks import TaskInput, message_text, parse_task_input, respond

CHAT_PATH = "/v1/chat/completions"
DEFAULT_COMPLETION_ESTIMATE = 200  # tokens charged up front when max_tokens is not set


# --- request / admin bodies -----------------------------------------------------

class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    content: str | list[Any] | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = None


class ConfigUpdate(BaseModel):
    """Settings that POST /admin/config can change at runtime. Unknown keys are rejected."""
    model_config = ConfigDict(extra="forbid")
    rpm: int | None = None
    tpm: int | None = None
    error_rate: float | None = None
    latency_median_s: float | None = None
    latency_sigma: float | None = None
    slow_tail_rate: float | None = None
    slow_tail_s: float | None = None
    stream_drop_rate: float | None = None


# --- helpers --------------------------------------------------------------------

def estimate_tokens(chars: int) -> int:
    return math.ceil(chars / 4)


def sample_latency(rng: random.Random, s: Settings) -> float:
    """Lognormal latency around the median, with an occasional much slower response."""
    latency = 0.0
    if s.latency_median_s > 0:
        latency = rng.lognormvariate(math.log(s.latency_median_s), s.latency_sigma)
    if rng.random() < s.slow_tail_rate:
        latency = s.slow_tail_s * rng.uniform(0.8, 1.25)
    return latency


def split_text(text: str, parts: int) -> list[str]:
    """Split text into `parts` contiguous pieces of near-equal size (fewer if text is short)."""
    parts = max(1, min(parts, len(text)))
    size, extra = divmod(len(text), parts)
    pieces, start = [], 0
    for i in range(parts):
        end = start + size + (1 if i < extra else 0)
        pieces.append(text[start:end])
        start = end
    return pieces


def sse(payload: dict[str, Any] | str) -> str:
    data = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    return f"data: {data}\n\n"


def error_body(kind: str, message: str) -> dict[str, Any]:
    return {"error": {"type": kind, "message": message}}


# --- provider state -------------------------------------------------------------

class Provider:
    """Everything that is shared across requests: config, limits, randomness, ledger."""

    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.limiter = RateLimiter(settings.rpm, settings.tpm, settings.burst_seconds, clock=clock)
        self.ledger = Ledger(settings.price_input_per_mtok, settings.price_output_per_mtok)
        self.rng = random.Random(settings.seed)

    async def reset(self) -> None:
        """Clear the ledger, refill the buckets and restart the random sequence."""
        self.ledger.clear()
        await self.limiter.reset()
        self.rng.seed(self.settings.seed)

    async def update(self, changes: dict[str, Any]) -> Settings:
        new = dataclasses.replace(self.settings, **changes)  # validates, may raise ValueError
        if (new.rpm, new.tpm) != (self.settings.rpm, self.settings.tpm):
            await self.limiter.reconfigure(new.rpm, new.tpm)
        self.settings = new
        return new


def create_app(settings: Settings | None = None,
               clock: Callable[[], float] = time.monotonic) -> FastAPI:
    provider = Provider(settings or Settings.from_env(), clock=clock)
    app = FastAPI(title="Mock LLM provider", version="1.0.0")
    app.state.provider = provider

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        """Chat requests that fail validation get an OpenAI-style 400 and a ledger entry."""
        if request.url.path != CHAT_PATH:
            return await request_validation_exception_handler(request, exc)
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'][1:]) or 'body'}: {err['msg']}"
            for err in exc.errors()[:3]
        )
        provider.ledger.record(
            ts=time.time(), ticket_id=request.headers.get("x-ticket-id"),
            run_id=request.headers.get("x-run-id"), task=None, status_code=400, prompt_tokens=0)
        return JSONResponse(error_body("invalid_request_error", problems), status_code=400)

    @app.post(CHAT_PATH)
    async def chat_completions(body: ChatRequest, request: Request):
        started = time.perf_counter()
        ts = time.time()
        s = provider.settings
        rng = provider.rng
        ticket_id = request.headers.get("x-ticket-id")
        run_id = request.headers.get("x-run-id")

        # 1. Token estimate from every message; the task comes from the last user message.
        texts = [message_text(m.content) for m in body.messages]
        prompt_tokens = estimate_tokens(sum(len(t) for t in texts))
        last_user = next((t for m, t in zip(reversed(body.messages), reversed(texts))
                          if m.role == "user"), "")
        task_input: TaskInput = parse_task_input(last_user)

        def record(status_code: int, **fields: Any) -> dict[str, Any]:
            return provider.ledger.record(
                ts=ts, ticket_id=ticket_id, run_id=run_id, task=task_input.task,
                status_code=status_code, prompt_tokens=prompt_tokens, stream=body.stream,
                latency_s=time.perf_counter() - started, **fields)

        # 2. Rate limits.
        decision: Decision = await provider.limiter.acquire(
            prompt_tokens + (body.max_tokens or DEFAULT_COMPLETION_ESTIMATE))
        headers = decision.headers()
        if not decision.allowed:
            record(429)
            what = " and ".join(decision.limited_by)
            message = (f"Rate limit reached for {what} per minute. "
                       f"Retry after {decision.retry_after_s} s.")
            return JSONResponse(error_body("rate_limit_error", message),
                                status_code=429, headers=headers)

        # 3. Random server errors.
        if rng.random() < s.error_rate:
            await asyncio.sleep(rng.uniform(0.05, 0.3))
            status = rng.choice((500, 503))
            message = ("The server had an error while processing your request."
                       if status == 500 else "The engine is currently overloaded, please retry later.")
            record(status)
            return JSONResponse(error_body("server_error", message),
                                status_code=status, headers=headers)

        # 4-5. Latency and answer.
        latency = sample_latency(rng, s)
        content = respond(task_input, ticket_id)
        completion_tokens = estimate_tokens(len(content))
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                 "total_tokens": prompt_tokens + completion_tokens}
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(ts)

        # 6a. Streaming (SSE).
        if body.stream:
            drop = rng.random() < s.stream_drop_rate
            pieces = split_text(content, 1 + rng.randint(8, 20))
            entry = record(200)  # updated in place as the stream progresses
            return StreamingResponse(
                stream_chunks(provider.ledger, entry, started, completion_id, created, body.model,
                              pieces, latency, usage, drop),
                media_type="text/event-stream",
                headers={**headers, "Cache-Control": "no-cache"},
            )

        # 6b. Plain JSON.
        await asyncio.sleep(latency)
        record(200, completion_tokens=completion_tokens)
        return JSONResponse({
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": body.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": usage,
        }, headers=headers)

    @app.get("/ledger")
    async def ledger_summary():
        return provider.ledger.summary()

    @app.get("/ledger/raw")
    async def ledger_raw():
        return {"entries": provider.ledger.entries}

    @app.post("/admin/reset")
    async def admin_reset():
        await provider.reset()
        return {"ok": True}

    @app.get("/admin/config")
    async def get_config():
        return dataclasses.asdict(provider.settings)

    @app.post("/admin/config")
    async def update_config(update: ConfigUpdate):
        try:
            new = await provider.update(update.model_dump(exclude_none=True))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return dataclasses.asdict(new)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app


async def stream_chunks(ledger: Ledger, entry: dict[str, Any], started: float, completion_id: str,
                        created: int, model: str, pieces: list[str], latency: float,
                        usage: dict[str, int], drop: bool) -> AsyncIterator[str]:
    """Yield SSE chunks: first piece after 25% of the latency, the rest spread over the remainder.

    A dropped stream stops after about half of the pieces, with no final chunk and no [DONE].
    The ledger entry is finalised however the stream ends (including a client disconnect).
    """
    def chunk(delta: dict[str, Any], finish_reason: str | None) -> dict[str, Any]:
        return {"id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}

    to_send = pieces[: max(1, len(pieces) // 2)] if drop else pieces
    gap = latency * 0.75 / max(1, len(pieces) - 1)
    sent_chars = 0
    try:
        await asyncio.sleep(latency * 0.25)
        for i, piece in enumerate(to_send):
            if i:
                await asyncio.sleep(gap)
            delta = {"role": "assistant", "content": piece} if i == 0 else {"content": piece}
            yield sse(chunk(delta, None))
            sent_chars += len(piece)
        if drop:
            entry["dropped"] = True
            return
        final = chunk({}, "stop")
        final["usage"] = usage
        yield sse(final)
        yield sse("[DONE]")
    finally:
        entry["completion_tokens"] = estimate_tokens(sent_chars)
        entry["latency_s"] = round(time.perf_counter() - started, 4)
        ledger.bill(entry)


app = create_app()
