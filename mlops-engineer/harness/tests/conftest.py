from __future__ import annotations

import pytest

from harness import DEFAULT_WORKLOAD_PATH
from harness import workload as workload_mod

import fakes


@pytest.fixture(scope="session")
def workload() -> dict:
    return workload_mod.load(DEFAULT_WORKLOAD_PATH)


@pytest.fixture
def services(workload):
    """Start fake SUT, LLM and store servers; yields a factory so a test can pick faults."""
    started: list[fakes.Server] = []

    def start(**sut_kwargs):
        sut, llm, commerce, servers = fakes.start_all(workload, **sut_kwargs)
        started.extend(servers)
        return sut, llm, commerce, servers

    yield start
    for server in started:
        server.stop()
