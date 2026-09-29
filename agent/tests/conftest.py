import copy

import httpx
import pytest

from app import config, llm, tools

from .fakes import ORDERS, FakeCommerce, FakeLLM


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(llm, "_http", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(config, "LLM_RETRY_DELAY_S", 0.0)
    return fake


@pytest.fixture
def fake_commerce(monkeypatch) -> FakeCommerce:
    fake = FakeCommerce(copy.deepcopy(ORDERS))
    monkeypatch.setattr(tools, "_http", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(config, "TOOL_RETRY_DELAY_S", 0.0)
    return fake
