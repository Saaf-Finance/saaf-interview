"""Benchmark harness for the returns agent: workload, load, approvals, chaos and verification."""

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_WORKLOAD_PATH = PROJECT_DIR / "data" / "workload.json"
