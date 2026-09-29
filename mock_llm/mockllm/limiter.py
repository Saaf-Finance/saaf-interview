"""Global token-bucket rate limiting on requests and tokens.

Two buckets are checked together: one counts requests, the other counts tokens
(prompt tokens + expected completion tokens). A request is admitted only if both
buckets have enough capacity; otherwise nothing is charged and the caller gets
the number of seconds to wait.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Callable

MAX_RETRY_AFTER_S = 10


class TokenBucket:
    """Classic token bucket: holds up to `capacity`, refills `refill_per_s` per second."""

    def __init__(self, capacity: float, refill_per_s: float, now: float) -> None:
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self.level = capacity
        self.updated = now

    def refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated)
        self.level = min(self.capacity, self.level + elapsed * self.refill_per_s)
        self.updated = now

    def seconds_until(self, amount: float) -> float:
        """Seconds until `amount` is available (call refill() first). inf if it never fits."""
        if amount <= self.level:
            return 0.0
        if amount > self.capacity:
            return math.inf
        return (amount - self.level) / self.refill_per_s


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: int  # 0 when allowed
    limited_by: tuple[str, ...]  # "requests" and/or "tokens"
    limit_requests: int
    remaining_requests: int
    limit_tokens: int
    remaining_tokens: int

    def headers(self) -> dict[str, str]:
        headers = {
            "x-ratelimit-limit-requests": str(self.limit_requests),
            "x-ratelimit-remaining-requests": str(self.remaining_requests),
            "x-ratelimit-limit-tokens": str(self.limit_tokens),
            "x-ratelimit-remaining-tokens": str(self.remaining_tokens),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after_s)
        return headers


class RateLimiter:
    """Requests-per-minute and tokens-per-minute limits shared by every caller."""

    def __init__(self, rpm: int, tpm: int, burst_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._burst_seconds = burst_seconds
        self.rpm = rpm
        self.tpm = tpm
        now = clock()
        self.requests = TokenBucket(rpm / 60 * burst_seconds, rpm / 60, now)
        self.tokens = TokenBucket(tpm / 60 * burst_seconds, tpm / 60, now)

    async def acquire(self, tokens: int) -> Decision:
        """Charge one request and `tokens` tokens if both fit; never waits."""
        async with self._lock:
            now = self._clock()
            self.requests.refill(now)
            self.tokens.refill(now)
            waits = {
                "requests": self.requests.seconds_until(1),
                "tokens": self.tokens.seconds_until(tokens),
            }
            limited_by = tuple(name for name, wait in waits.items() if wait > 0)
            if limited_by:
                wait = min(max(waits.values()), MAX_RETRY_AFTER_S)
                retry_after = min(MAX_RETRY_AFTER_S, max(1, math.ceil(wait)))
                return self._decision(False, retry_after, limited_by)
            self.requests.level -= 1
            self.tokens.level -= tokens
            return self._decision(True, 0, ())

    async def reconfigure(self, rpm: int, tpm: int) -> None:
        """Apply new limits; current levels are kept but clamped to the new capacity."""
        async with self._lock:
            now = self._clock()
            for bucket, per_minute in ((self.requests, rpm), (self.tokens, tpm)):
                bucket.refill(now)
                bucket.capacity = per_minute / 60 * self._burst_seconds
                bucket.refill_per_s = per_minute / 60
                bucket.level = min(bucket.level, bucket.capacity)
            self.rpm, self.tpm = rpm, tpm

    async def reset(self) -> None:
        """Refill both buckets to capacity."""
        async with self._lock:
            now = self._clock()
            for bucket in (self.requests, self.tokens):
                bucket.level = bucket.capacity
                bucket.updated = now

    def _decision(self, allowed: bool, retry_after: int, limited_by: tuple[str, ...]) -> Decision:
        return Decision(
            allowed=allowed,
            retry_after_s=retry_after,
            limited_by=limited_by,
            limit_requests=self.rpm,
            remaining_requests=max(0, math.floor(self.requests.level)),
            limit_tokens=self.tpm,
            remaining_tokens=max(0, math.floor(self.tokens.level)),
        )
