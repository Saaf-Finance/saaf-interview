"""Provider settings, loaded from MOCK_LLM_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Mapping

ENV_PREFIX = "MOCK_LLM_"


@dataclass(frozen=True)
class Settings:
    rpm: int = 1200  # requests per minute
    tpm: int = 600_000  # tokens per minute
    error_rate: float = 0.02  # share of requests answered with 500/503
    latency_median_s: float = 0.8  # median of the lognormal latency
    latency_sigma: float = 0.6  # sigma of the lognormal latency
    slow_tail_rate: float = 0.01  # share of requests that take ~slow_tail_s
    slow_tail_s: float = 8.0
    stream_drop_rate: float = 0.0  # share of streams cut off half-way
    seed: int = 7
    price_input_per_mtok: float = 3.0  # USD per million prompt tokens
    price_output_per_mtok: float = 15.0  # USD per million completion tokens
    burst_seconds: float = 5.0  # bucket capacity, in seconds of refill

    def __post_init__(self) -> None:
        if self.rpm <= 0 or self.tpm <= 0:
            raise ValueError("rpm and tpm must be > 0")
        if self.burst_seconds <= 0:
            raise ValueError("burst_seconds must be > 0")
        for name in ("error_rate", "slow_tail_rate", "stream_drop_rate"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        for name in ("latency_median_s", "latency_sigma", "slow_tail_s",
                     "price_input_per_mtok", "price_output_per_mtok"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from MOCK_LLM_<FIELD> variables; unset ones keep their defaults."""
        env = os.environ if env is None else env
        values: dict[str, int | float] = {}
        for f in fields(cls):
            raw = env.get(ENV_PREFIX + f.name.upper(), "").strip()
            if raw:
                values[f.name] = int(raw) if f.type == "int" else float(raw)
        return cls(**values)
