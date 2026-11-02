import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway
import mock_providers
from mock_providers import DEFAULT_DELAY_SECONDS, backup_app, primary_app
from rate_limiter import count_tokens

pytestmark = pytest.mark.asyncio

REQUEST = {"prompt": "hello mock provider", "max_tokens": 50}


@pytest.fixture(autouse=True)
def clear_mock_settings(monkeypatch):
    monkeypatch.delenv("MOCK_PRIMARY_MODE", raising=False)
    monkeypatch.delenv("MOCK_BACKUP_MODE", raising=False)
    monkeypatch.delenv("MOCK_DELAY_SECONDS", raising=False)


async def post(app, payload=REQUEST):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        return await client.post("/v1/completions", json=payload)


@pytest.mark.parametrize(
    ("app", "model"),
    [(primary_app, "mock-primary-1"), (backup_app, "mock-backup-1")],
    ids=["primary", "backup"],
)
async def test_providers_succeed_by_default(app, model):
    response = await post(app)

    assert response.status_code == 200
    assert response.json()["model"] == model
    assert REQUEST["prompt"] in response.json()["text"]


async def test_usage_matches_gateway_tokenizer():
    body = (await post(primary_app)).json()

    assert body["usage"]["prompt_tokens"] == count_tokens(REQUEST["prompt"])
    assert body["usage"]["completion_tokens"] == count_tokens(body["text"])
    assert body["usage"]["total_tokens"] == (
        body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    )


async def test_prompt_tokens_are_not_whitespace_based():
    prompt = "unbroken" * 50
    body = (await post(primary_app, {"prompt": prompt, "max_tokens": 5})).json()

    assert body["usage"]["prompt_tokens"] == count_tokens(prompt)
    assert body["usage"]["prompt_tokens"] > 1


async def test_completion_tokens_respect_max_tokens():
    body = (await post(primary_app, {"prompt": REQUEST["prompt"], "max_tokens": 1})).json()
    assert body["usage"]["completion_tokens"] == 1


@pytest.mark.parametrize(
    ("app", "mode_variable"),
    [(primary_app, "MOCK_PRIMARY_MODE"), (backup_app, "MOCK_BACKUP_MODE")],
    ids=["primary", "backup"],
)
@pytest.mark.parametrize(("mode", "status_code"), [("rate_limit", 429), ("fail", 500)])
async def test_failure_modes(monkeypatch, app, mode_variable, mode, status_code):
    monkeypatch.setenv(mode_variable, mode)
    assert (await post(app)).status_code == status_code


async def test_modes_are_independent(monkeypatch):
    monkeypatch.setenv("MOCK_PRIMARY_MODE", "rate_limit")

    assert (await post(primary_app)).status_code == 429
    assert (await post(backup_app)).status_code == 200


async def test_timeout_mode_waits_for_configured_delay(monkeypatch):
    delays = []

    async def record_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setenv("MOCK_PRIMARY_MODE", "timeout")
    monkeypatch.setenv("MOCK_DELAY_SECONDS", "7.5")
    monkeypatch.setattr(mock_providers.asyncio, "sleep", record_sleep)

    response = await post(primary_app)

    assert 7.5 in delays
    assert response.status_code == 200


async def test_default_delay_exceeds_primary_timeout():
    assert DEFAULT_DELAY_SECONDS > gateway.PRIMARY_TIMEOUT_SECONDS


async def test_successful_response_satisfies_gateway_contract():
    body = (await post(primary_app)).json()

    completion = gateway.ProviderCompletion.model_validate(body)

    assert gateway.reported_tokens(completion.usage) == body["usage"]["total_tokens"]


async def test_reported_usage_is_never_below_gateway_input_estimate():
    body = (await post(backup_app)).json()
    assert body["usage"]["total_tokens"] >= count_tokens(REQUEST["prompt"])
