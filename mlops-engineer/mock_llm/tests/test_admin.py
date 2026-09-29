"""/admin/config, /admin/reset, /healthz and settings loading."""

import time

import pytest

from helpers import chat, prompt
from mockllm.settings import Settings

TICKET = {"ticket_id": "T-00005", "message": "refund please"}


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_get_config(client):
    config = client.get("/admin/config").json()
    assert config["rpm"] == 1200 and config["tpm"] == 600_000
    assert config["seed"] == 7 and config["burst_seconds"] == 5
    assert config["price_input_per_mtok"] == 3.0 and config["price_output_per_mtok"] == 15.0


def test_partial_update_returns_new_config(client):
    r = client.post("/admin/config", json={"rpm": 60, "error_rate": 0.5})
    assert r.status_code == 200
    body = r.json()
    assert body["rpm"] == 60 and body["error_rate"] == 0.5
    assert body["tpm"] == 600_000  # untouched
    assert client.get("/admin/config").json() == body


@pytest.mark.parametrize("payload", [
    {"error_rate": 1.5}, {"rpm": 0}, {"latency_median_s": -1}, {"stream_drop_rate": -0.1},
    {"seed": 3}, {"price_input_per_mtok": 1.0}, {"not_a_setting": 1}, {"rpm": "fast"},
])
def test_invalid_updates_are_rejected(client, payload):
    before = client.get("/admin/config").json()
    assert client.post("/admin/config", json=payload).status_code == 422
    assert client.get("/admin/config").json() == before


def test_rpm_change_takes_effect(client):
    codes = [chat(client, prompt("classify", TICKET)).status_code for _ in range(8)]
    assert codes == [200] * 8
    client.post("/admin/config", json={"rpm": 60})  # capacity drops to 5, level clamped
    codes = [chat(client, prompt("classify", TICKET)).status_code for _ in range(8)]
    assert codes == [200] * 5 + [429] * 3
    r = chat(client, prompt("classify", TICKET))
    assert r.headers["x-ratelimit-limit-requests"] == "60"


def test_tpm_change_takes_effect(client):
    assert chat(client, prompt("classify", TICKET), max_tokens=500).status_code == 200
    client.post("/admin/config", json={"tpm": 600})  # 50 tokens of burst
    r = chat(client, prompt("classify", TICKET), max_tokens=500)
    assert r.status_code == 429 and r.headers["x-ratelimit-limit-tokens"] == "600"


def test_error_rate_change_takes_effect(client):
    assert chat(client, prompt("classify", TICKET)).status_code == 200
    client.post("/admin/config", json={"error_rate": 1.0})
    assert chat(client, prompt("classify", TICKET)).status_code in (500, 503)
    client.post("/admin/config", json={"error_rate": 0.0})
    assert chat(client, prompt("classify", TICKET)).status_code == 200


def test_latency_change_takes_effect(client):
    client.post("/admin/config", json={"latency_median_s": 0.3, "latency_sigma": 0.0})
    started = time.perf_counter()
    chat(client, prompt("classify", TICKET))
    assert time.perf_counter() - started >= 0.3


def test_stream_drop_change_takes_effect(client):
    client.post("/admin/config", json={"stream_drop_rate": 1.0})
    r = chat(client, prompt("classify", TICKET), stream=True)
    assert "[DONE]" not in r.text
    assert client.get("/ledger").json()["dropped_streams"] == 1


def test_reset_clears_ledger_and_refills_buckets(make_client):
    client = make_client(rpm=60)
    for _ in range(7):
        chat(client, prompt("classify", TICKET))
    assert client.get("/ledger").json()["calls"] == 7
    assert client.post("/admin/reset").json() == {"ok": True}
    assert client.get("/ledger").json() == {
        "calls": 0, "by_status": {}, "prompt_tokens": 0, "completion_tokens": 0,
        "cost_usd": 0.0, "dropped_streams": 0, "per_ticket": {}}
    assert client.get("/ledger/raw").json() == {"entries": []}
    codes = [chat(client, prompt("classify", TICKET)).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    assert client.get("/admin/config").json()["rpm"] == 60  # config is kept


def test_reset_restarts_random_sequence(make_client):
    client = make_client(error_rate=0.5)

    def outcomes():
        return [chat(client, prompt("classify", TICKET)).status_code for _ in range(20)]

    first = outcomes()
    client.post("/admin/reset")
    assert outcomes() == first
    assert set(first) - {200}  # some failures happened


def test_settings_from_env():
    s = Settings.from_env({"MOCK_LLM_RPM": "90", "MOCK_LLM_ERROR_RATE": "0.1",
                           "MOCK_LLM_LATENCY_MEDIAN_S": "0.05", "MOCK_LLM_SEED": "3",
                           "MOCK_LLM_BURST_SECONDS": "2", "UNRELATED": "x",
                           "MOCK_LLM_TPM": ""})
    assert (s.rpm, s.error_rate, s.latency_median_s, s.seed, s.burst_seconds) == (
        90, 0.1, 0.05, 3, 2.0)
    assert s.tpm == 600_000 and Settings.from_env({}) == Settings()


def test_settings_reject_bad_env():
    with pytest.raises(ValueError):
        Settings.from_env({"MOCK_LLM_ERROR_RATE": "2"})
    with pytest.raises(ValueError):
        Settings.from_env({"MOCK_LLM_RPM": "lots"})
