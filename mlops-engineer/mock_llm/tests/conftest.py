import pytest
from fastapi.testclient import TestClient

from helpers import QUIET, FakeClock
from mockllm.app import create_app
from mockllm.settings import Settings


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_client(clock):
    """Factory for a TestClient around a fresh app; keyword args override Settings."""
    clients = []

    def _make(**overrides) -> TestClient:
        settings = Settings(**{**QUIET, **overrides})
        client = TestClient(create_app(settings, clock=clock))
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()
