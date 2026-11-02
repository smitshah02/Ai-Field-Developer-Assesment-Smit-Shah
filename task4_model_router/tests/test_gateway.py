import asyncio
import contextlib
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway
from gateway import MAX_OUTPUT_TOKENS, MAX_PROMPT_CHARS, MAX_REPORTED_TOKENS, Attempt, ProviderCompletion
from rate_limiter import TokenRateLimiter, count_tokens

pytestmark = pytest.mark.asyncio

CONFIGURED_PRIMARY_TIMEOUT = gateway.PRIMARY_TIMEOUT_SECONDS
CONFIGURED_BACKUP_TIMEOUT = gateway.BACKUP_TIMEOUT_SECONDS

PRIMARY_URL = "http://primary.test/v1/completions"
BACKUP_URL = "http://backup.test/v1/completions"
LONG_TIMEOUT = 5.0
SHORT_TIMEOUT = 0.1
EVENT_WAIT_SECONDS = 5.0

KEY_A = {"x-api-key": "key-a"}
KEY_B = {"x-api-key": "key-b"}
PROMPT = "hello world"
MAX_TOKENS = 10
REQUEST = {"prompt": PROMPT, "max_tokens": MAX_TOKENS}
INPUT_TOKENS = count_tokens(PROMPT)
RESERVED_TOKENS = INPUT_TOKENS + MAX_TOKENS

SECRET = "sk-upstream-secret"
INTERNAL_PATH = "/srv/provider/app.py"
SECRET_DETAIL = f"Traceback (most recent call last): {INTERNAL_PATH} key={SECRET}"
LEAK_MARKERS = (SECRET, INTERNAL_PATH, "Traceback", "primary.test", "backup.test")


def ok(usage=None):
    body = {"text": "generated", "model": "fake-model"}
    if usage is not None:
        body["usage"] = usage

    async def respond(request):
        return httpx.Response(200, json=body)

    return respond


def total(tokens):
    return {"total_tokens": tokens}


def status(code, content=b"{}"):
    async def respond(request):
        return httpx.Response(code, content=content)

    return respond


def raw(content):
    return status(200, content)


def raises(error):
    async def respond(request):
        raise error

    return respond


async def hang(request):
    await asyncio.Event().wait()


def delayed(behavior, seconds):
    async def respond(request):
        await asyncio.sleep(seconds)
        return await behavior(request)

    return respond


class DripStream(httpx.AsyncByteStream):
    def __init__(self):
        self.chunks_sent = 0
        self.closed = False

    async def __aiter__(self):
        while True:
            await asyncio.sleep(0.005)
            self.chunks_sent += 1
            yield b" "

    async def aclose(self):
        self.closed = True


class FakeProvider:
    def __init__(self):
        self.behavior = ok(total(RESERVED_TOKENS))
        self.calls = []
        self.headers = []
        self.entered = asyncio.Event()
        self.cancelled = False

    async def handle(self, request):
        self.calls.append(json.loads(request.content))
        self.headers.append(request.headers)
        self.entered.set()
        try:
            return await self.behavior(request)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@dataclass
class Router:
    client: httpx.AsyncClient
    limiter: TokenRateLimiter
    primary: FakeProvider
    backup: FakeProvider
    db_path: str

    async def post(self, payload=REQUEST, headers=KEY_A):
        return await self.client.post("/v1/completions", json=payload, headers=headers)

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            return connection.execute(
                "SELECT tenant_id, tokens FROM token_usage ORDER BY id"
            ).fetchall()

    def provider_calls(self):
        return len(self.primary.calls) + len(self.backup.calls)


def assert_error(response, status_code, code):
    assert response.status_code == status_code
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]


def assert_no_leaks(response):
    for marker in LEAK_MARKERS:
        assert marker not in response.text


def count_calls(monkeypatch, limiter, name):
    original = getattr(limiter, name)
    calls = []

    async def wrapper(*args):
        calls.append(args)
        return await original(*args)

    monkeypatch.setattr(limiter, name, wrapper)
    return calls


def use_short_timeouts(monkeypatch):
    monkeypatch.setattr(gateway, "PRIMARY_TIMEOUT_SECONDS", SHORT_TIMEOUT)
    monkeypatch.setattr(gateway, "BACKUP_TIMEOUT_SECONDS", SHORT_TIMEOUT)


def fixed_input_tokens(monkeypatch, tokens):
    monkeypatch.setattr(gateway, "count_tokens", lambda text: tokens)


@pytest_asyncio.fixture
async def router(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "PRIMARY_URL", PRIMARY_URL)
    monkeypatch.setattr(gateway, "BACKUP_URL", BACKUP_URL)
    monkeypatch.setattr(gateway, "PRIMARY_TIMEOUT_SECONDS", LONG_TIMEOUT)
    monkeypatch.setattr(gateway, "BACKUP_TIMEOUT_SECONDS", LONG_TIMEOUT)
    monkeypatch.setattr(gateway, "TENANT_API_KEYS", {"key-a": "tenant-a", "key-b": "tenant-b"})

    primary = FakeProvider()
    backup = FakeProvider()

    async def route(request):
        provider = primary if request.url.host == "primary.test" else backup
        return await provider.handle(request)

    db_path = str(tmp_path / "gateway.db")
    limiter = TokenRateLimiter(db_path)
    await limiter.open()
    async with httpx.AsyncClient(transport=httpx.MockTransport(route), timeout=None) as provider_client:
        gateway.app.state.limiter = limiter
        gateway.app.state.provider_client = provider_client
        transport = httpx.ASGITransport(app=gateway.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            yield Router(client, limiter, primary, backup, db_path)
    await limiter.close()


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"total_tokens": 1}, 1),
        ({"total_tokens": 150}, 150),
        ({"total_tokens": MAX_REPORTED_TOKENS}, MAX_REPORTED_TOKENS),
        ({"total_tokens": MAX_REPORTED_TOKENS + 1}, None),
        ({"total_tokens": 10**30}, None),
        ({"total_tokens": 2**62}, None),
        ({"total_tokens": 0}, None),
        ({"total_tokens": -5}, None),
        ({"total_tokens": 9.5}, None),
        ({"total_tokens": "9"}, None),
        ({"total_tokens": True}, None),
        ({"total_tokens": None}, None),
        ({"prompt_tokens": 3, "completion_tokens": 4}, None),
        ({}, None),
        ([], None),
        ([1], None),
        ("9", None),
        (9, None),
        (None, None),
    ],
)
async def test_reported_tokens(usage, expected):
    assert gateway.reported_tokens(usage) == expected


def succeeded(tokens=None):
    usage = None if tokens is None else total(tokens)
    completion = ProviderCompletion(text="generated", model="fake-model", usage=usage)
    return Attempt(gateway.SUCCESS, maybe_processed=True, completion=completion)


RATE_LIMITED = Attempt(gateway.RATE_LIMITED, maybe_processed=False)
TIMED_OUT = Attempt(gateway.TIMED_OUT, maybe_processed=True)
REJECTED = Attempt(gateway.FAILED, maybe_processed=False)
UNREACHABLE = Attempt(gateway.UNAVAILABLE, maybe_processed=False)
BROKEN_CONNECTION = Attempt(gateway.UNAVAILABLE, maybe_processed=True)


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        ([succeeded(150)], 150),
        ([succeeded(50)], 100),
        ([succeeded(100)], 100),
        ([succeeded(500)], 500),
        ([succeeded()], 356),
        ([RATE_LIMITED, succeeded(150)], 150),
        ([RATE_LIMITED, succeeded(50)], 100),
        ([RATE_LIMITED, succeeded()], 356),
        ([TIMED_OUT, succeeded(150)], 356),
        ([TIMED_OUT, succeeded(500)], 500),
        ([TIMED_OUT, succeeded()], 356),
        ([REJECTED], 0),
        ([UNREACHABLE], 0),
        ([RATE_LIMITED, REJECTED], 0),
        ([RATE_LIMITED, UNREACHABLE], 0),
        ([BROKEN_CONNECTION], 356),
        ([TIMED_OUT, REJECTED], 356),
        ([RATE_LIMITED, TIMED_OUT], 356),
        ([TIMED_OUT, TIMED_OUT], 356),
    ],
)
async def test_tokens_to_charge(attempts, expected):
    assert gateway.tokens_to_charge(100, 356, attempts) == expected


async def test_primary_success_returns_primary_completion(router):
    response = await router.post()

    assert response.status_code == 200
    assert response.json() == {
        "text": "generated",
        "model": "fake-model",
        "provider": "primary",
        "tokens_charged": RESERVED_TOKENS,
    }
    assert len(router.primary.calls) == 1
    assert router.backup.calls == []


async def test_request_body_is_forwarded_to_provider(router):
    await router.post()
    assert router.primary.calls == [REQUEST]


async def test_default_max_tokens_is_forwarded_and_reserved(router):
    router.primary.behavior = ok()
    response = await router.post({"prompt": PROMPT})

    assert router.primary.calls == [{"prompt": PROMPT, "max_tokens": gateway.DEFAULT_OUTPUT_TOKENS}]
    assert response.json()["tokens_charged"] == INPUT_TOKENS + gateway.DEFAULT_OUTPUT_TOKENS


async def test_unknown_request_fields_are_not_forwarded(router):
    await router.post({**REQUEST, "model": "other", "url": "http://attacker.test"})
    assert router.primary.calls == [REQUEST]


async def test_tenant_api_key_is_not_forwarded_to_provider(router):
    await router.post()
    assert "x-api-key" not in router.primary.headers[0]


async def test_primary_429_uses_backup(router):
    router.primary.behavior = status(429)
    router.backup.behavior = ok(total(RESERVED_TOKENS))

    response = await router.post()

    assert response.status_code == 200
    assert response.json()["provider"] == "backup"
    assert len(router.primary.calls) == 1
    assert router.backup.calls == [REQUEST]


async def test_primary_timeout_uses_backup_and_cancels_primary(router, monkeypatch):
    use_short_timeouts(monkeypatch)
    monkeypatch.setattr(gateway, "BACKUP_TIMEOUT_SECONDS", LONG_TIMEOUT)
    router.primary.behavior = hang

    response = await router.post()

    assert response.status_code == 200
    assert response.json()["provider"] == "backup"
    assert router.primary.cancelled
    assert router.backup.calls == [REQUEST]


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")])
async def test_httpx_timeout_errors_use_backup(router, error):
    router.primary.behavior = raises(error)

    response = await router.post()

    assert response.status_code == 200
    assert response.json()["provider"] == "backup"


async def test_response_before_deadline_stays_on_primary(router):
    router.primary.behavior = delayed(ok(total(RESERVED_TOKENS)), 0.01)

    response = await router.post()

    assert response.json()["provider"] == "primary"
    assert router.backup.calls == []


async def test_configured_timeouts_are_passed_to_each_attempt(router, monkeypatch):
    monkeypatch.setattr(gateway, "PRIMARY_TIMEOUT_SECONDS", CONFIGURED_PRIMARY_TIMEOUT)
    monkeypatch.setattr(gateway, "BACKUP_TIMEOUT_SECONDS", CONFIGURED_BACKUP_TIMEOUT)
    original = gateway.attempt_provider
    seen = []

    async def spy(client, url, payload, timeout_seconds):
        seen.append((url, timeout_seconds))
        return await original(client, url, payload, timeout_seconds)

    monkeypatch.setattr(gateway, "attempt_provider", spy)
    router.primary.behavior = status(429)

    await router.post()

    assert seen == [
        (PRIMARY_URL, 3.0),
        (BACKUP_URL, CONFIGURED_BACKUP_TIMEOUT),
    ]


async def test_primary_times_out_after_3000_milliseconds(router, monkeypatch):
    monkeypatch.setattr(gateway, "PRIMARY_TIMEOUT_SECONDS", CONFIGURED_PRIMARY_TIMEOUT)
    router.primary.behavior = hang
    loop = asyncio.get_running_loop()

    started_at = loop.time()
    response = await router.post()
    elapsed = loop.time() - started_at

    assert response.json()["provider"] == "backup"
    assert 2.99 <= elapsed < 4.0


async def test_slow_drip_exceeding_total_timeout_uses_backup(router, monkeypatch):
    monkeypatch.setattr(gateway, "PRIMARY_TIMEOUT_SECONDS", 0.1)
    stream = DripStream()

    async def drip(request):
        return httpx.Response(200, stream=stream)

    router.primary.behavior = drip

    response = await router.post()

    assert response.status_code == 200
    assert response.json()["provider"] == "backup"
    assert stream.chunks_sent >= 2
    assert stream.closed


@pytest.mark.parametrize("code", [302, 400, 401, 404, 500, 502, 503])
async def test_other_primary_statuses_do_not_use_backup(router, code):
    router.primary.behavior = status(code)

    response = await router.post()

    assert_error(response, 502, "PROVIDER_ERROR")
    assert router.backup.calls == []
    assert router.rows() == [("tenant-a", 0)]


async def test_primary_connection_failure_does_not_use_backup(router):
    router.primary.behavior = raises(httpx.ConnectError("refused"))

    response = await router.post()

    assert_error(response, 502, "PROVIDER_UNAVAILABLE")
    assert router.backup.calls == []
    assert router.rows() == [("tenant-a", 0)]


@pytest.mark.parametrize(
    "error",
    [httpx.ReadError("reset"), httpx.RemoteProtocolError("bad frame")],
    ids=["read_error", "protocol_error"],
)
async def test_primary_connection_lost_keeps_reservation(router, error):
    router.primary.behavior = raises(error)

    response = await router.post()

    assert_error(response, 502, "PROVIDER_UNAVAILABLE")
    assert router.backup.calls == []
    assert router.rows() == [("tenant-a", RESERVED_TOKENS)]


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"[]",
        b"null",
        b"{}",
        b'{"text": "missing model"}',
        b'{"model": "missing text"}',
        b'{"text": 1, "model": "fake-model"}',
        b'{"text": "generated", "model": 1}',
    ],
)
async def test_invalid_primary_body_is_rejected_and_keeps_reservation(router, content):
    router.primary.behavior = raw(content)

    response = await router.post()

    assert_error(response, 502, "PROVIDER_ERROR")
    assert router.backup.calls == []
    assert router.rows() == [("tenant-a", RESERVED_TOKENS)]


@pytest.mark.parametrize(
    ("backup_behavior", "status_code", "code", "backup_may_have_run"),
    [
        (status(500), 502, "ALL_PROVIDERS_FAILED", False),
        (status(429), 502, "ALL_PROVIDERS_FAILED", False),
        (raises(httpx.ConnectError("refused")), 502, "ALL_PROVIDERS_FAILED", False),
        (raises(httpx.ReadError("reset")), 502, "ALL_PROVIDERS_FAILED", True),
        (raw(b"not json"), 502, "ALL_PROVIDERS_FAILED", True),
        (hang, 504, "GATEWAY_TIMEOUT", True),
    ],
    ids=["http_500", "http_429", "connect_error", "read_error", "invalid_body", "timeout"],
)
@pytest.mark.parametrize(
    ("primary_behavior", "primary_may_have_run"),
    [(status(429), False), (hang, True)],
    ids=["primary_429", "primary_timeout"],
)
async def test_both_providers_failing(
    router,
    monkeypatch,
    primary_behavior,
    primary_may_have_run,
    backup_behavior,
    status_code,
    code,
    backup_may_have_run,
):
    use_short_timeouts(monkeypatch)
    router.primary.behavior = primary_behavior
    router.backup.behavior = backup_behavior

    response = await router.post()

    assert_error(response, status_code, code)
    expected = RESERVED_TOKENS if primary_may_have_run or backup_may_have_run else 0
    assert router.rows() == [("tenant-a", expected)]
    assert len(router.primary.calls) == 1
    assert len(router.backup.calls) == 1


@pytest.mark.parametrize(
    "primary_behavior",
    [status(429), hang],
    ids=["primary_429", "primary_timeout"],
)
async def test_fallback_reserves_and_settles_exactly_once(router, monkeypatch, primary_behavior):
    monkeypatch.setattr(gateway, "PRIMARY_TIMEOUT_SECONDS", SHORT_TIMEOUT)
    reserve_calls = count_calls(monkeypatch, router.limiter, "reserve")
    settle_calls = count_calls(monkeypatch, router.limiter, "settle")
    router.primary.behavior = primary_behavior

    response = await router.post()

    assert response.status_code == 200
    assert len(reserve_calls) == 1
    assert len(settle_calls) == 1
    assert len(router.rows()) == 1


async def test_usage_below_input_estimate_charges_input_estimate(router, monkeypatch):
    fixed_input_tokens(monkeypatch, 100)
    router.primary.behavior = ok(total(5))

    response = await router.post()

    assert response.json()["tokens_charged"] == 100
    assert router.rows() == [("tenant-a", 100)]


async def test_usage_between_input_and_reservation_charges_reported(router, monkeypatch):
    fixed_input_tokens(monkeypatch, 100)
    router.primary.behavior = ok(total(105))

    response = await router.post()

    assert response.json()["tokens_charged"] == 105
    assert router.rows() == [("tenant-a", 105)]


async def test_usage_above_reservation_charges_reported(router, monkeypatch):
    fixed_input_tokens(monkeypatch, 100)
    router.primary.behavior = ok(total(5_000))

    response = await router.post()

    assert response.json()["tokens_charged"] == 5_000
    assert router.rows() == [("tenant-a", 5_000)]


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"total_tokens": "9"},
        {"total_tokens": 9.5},
        {"total_tokens": 0},
        {"total_tokens": -1},
        {"total_tokens": True},
        {"total_tokens": 10**30},
        {"total_tokens": MAX_REPORTED_TOKENS + 1},
        [1],
    ],
    ids=[
        "missing",
        "empty",
        "string",
        "float",
        "zero",
        "negative",
        "boolean",
        "overflow",
        "over_maximum",
        "list",
    ],
)
async def test_unusable_usage_keeps_full_reservation(router, usage):
    router.primary.behavior = ok(usage)

    response = await router.post()

    assert response.status_code == 200
    assert response.json()["tokens_charged"] == RESERVED_TOKENS
    assert router.rows() == [("tenant-a", RESERVED_TOKENS)]


async def test_oversized_usage_does_not_block_tenant(router):
    router.primary.behavior = ok(total(2**62))

    assert (await router.post()).status_code == 200
    assert (await router.post()).status_code == 200


async def test_backup_usage_after_primary_429_is_reconciled(router, monkeypatch):
    fixed_input_tokens(monkeypatch, 100)
    router.primary.behavior = status(429)
    router.backup.behavior = ok(total(105))

    response = await router.post()

    assert response.json()["tokens_charged"] == 105
    assert router.rows() == [("tenant-a", 105)]


async def test_backup_usage_after_primary_429_cannot_go_below_input(router, monkeypatch):
    fixed_input_tokens(monkeypatch, 100)
    router.primary.behavior = status(429)
    router.backup.behavior = ok(total(5))

    response = await router.post()

    assert response.json()["tokens_charged"] == 100


@pytest.mark.parametrize(
    ("backup_usage", "expected"),
    [
        (total(1), RESERVED_TOKENS),
        (None, RESERVED_TOKENS),
        (total(RESERVED_TOKENS + 500), RESERVED_TOKENS + 500),
    ],
    ids=["low_usage", "missing_usage", "high_usage"],
)
async def test_primary_timeout_charges_conservatively_once(router, monkeypatch, backup_usage, expected):
    monkeypatch.setattr(gateway, "PRIMARY_TIMEOUT_SECONDS", SHORT_TIMEOUT)
    router.primary.behavior = hang
    router.backup.behavior = ok(backup_usage)

    response = await router.post()

    assert response.json()["tokens_charged"] == expected
    assert router.rows() == [("tenant-a", expected)]


async def test_low_reported_usage_returns_quota_to_tenant(router, monkeypatch):
    fixed_input_tokens(monkeypatch, 40_000)
    router.primary.behavior = ok(total(40_000))

    payload = {"prompt": PROMPT, "max_tokens": MAX_OUTPUT_TOKENS}
    assert (await router.post(payload)).status_code == 200

    assert await router.limiter.reserve("tenant-a", 10_000) is not None


async def test_exhausted_quota_rejects_before_any_provider_call(router):
    await router.limiter.reserve("tenant-a", 50_000)

    response = await router.post()

    assert_error(response, 429, "RATE_LIMIT_EXCEEDED")
    assert router.provider_calls() == 0
    assert router.rows() == [("tenant-a", 50_000)]


async def test_request_larger_than_window_is_rejected_before_any_provider_call(router, monkeypatch):
    fixed_input_tokens(monkeypatch, 49_991)

    response = await router.post()

    assert_error(response, 429, "RATE_LIMIT_EXCEEDED")
    assert router.provider_calls() == 0
    assert router.rows() == []


async def test_request_exactly_filling_window_is_allowed(router, monkeypatch):
    fixed_input_tokens(monkeypatch, 49_990)
    router.primary.behavior = ok()

    response = await router.post()

    assert response.status_code == 200
    assert router.rows() == [("tenant-a", 50_000)]


async def test_tenant_over_quota_does_not_affect_other_tenant(router):
    await router.limiter.reserve("tenant-a", 50_000)

    assert (await router.post(headers=KEY_A)).status_code == 429
    assert (await router.post(headers=KEY_B)).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [{}, {"x-api-key": ""}, {"x-api-key": "wrong"}, {"authorization": "Bearer key-a"}],
    ids=["missing", "empty", "unknown", "wrong_header"],
)
async def test_invalid_api_key_is_rejected(router, headers):
    response = await router.post(headers=headers)

    assert_error(response, 401, "INVALID_API_KEY")
    assert router.provider_calls() == 0
    assert router.rows() == []


async def test_api_key_is_checked_before_request_body(router):
    response = await router.client.post("/v1/completions", content=b"{bad")
    assert_error(response, 401, "INVALID_API_KEY")


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{bad",
        b"\xff\xfe",
        b"[]",
        b"null",
        b"{}",
        b'{"max_tokens": 5}',
        b'{"prompt": ""}',
        b'{"prompt": 123}',
        b'{"prompt": null}',
        b'{"prompt": ["hello"]}',
        b'{"prompt": "hello", "max_tokens": 0}',
        b'{"prompt": "hello", "max_tokens": -1}',
        b'{"prompt": "hello", "max_tokens": 4097}',
        b'{"prompt": "hello", "max_tokens": "5"}',
        b'{"prompt": "hello", "max_tokens": 1.5}',
        b'{"prompt": "hello", "max_tokens": true}',
        b'{"prompt": "hello", "max_tokens": null}',
    ],
)
async def test_invalid_request_body_is_rejected(router, content):
    response = await router.client.post("/v1/completions", content=content, headers=KEY_A)

    assert_error(response, 400, "INVALID_REQUEST")
    assert router.provider_calls() == 0
    assert router.rows() == []


async def test_prompt_over_maximum_length_is_rejected(router):
    response = await router.post({"prompt": "a" * (MAX_PROMPT_CHARS + 1)})

    assert_error(response, 400, "INVALID_REQUEST")
    assert router.provider_calls() == 0


async def test_prompt_at_maximum_length_is_accepted(router, monkeypatch):
    fixed_input_tokens(monkeypatch, 1)

    response = await router.post({"prompt": "a" * MAX_PROMPT_CHARS})

    assert response.status_code == 200


@pytest.mark.parametrize("max_tokens", [1, MAX_OUTPUT_TOKENS])
async def test_max_tokens_boundaries_are_accepted(router, max_tokens):
    response = await router.post({"prompt": PROMPT, "max_tokens": max_tokens})
    assert response.status_code == 200


async def test_special_token_text_in_prompt_is_accepted(router):
    response = await router.post({"prompt": "<|endoftext|>", "max_tokens": 1})
    assert response.status_code == 200


async def test_unknown_route_uses_standard_error(router):
    response = await router.client.get("/unknown")
    assert_error(response, 404, "HTTP_ERROR")


async def test_wrong_method_uses_standard_error(router):
    response = await router.client.get("/v1/completions")
    assert_error(response, 405, "HTTP_ERROR")


LEAKY_FAILURES = [
    status(500, SECRET_DETAIL.encode()),
    status(429, SECRET_DETAIL.encode()),
    raw(SECRET_DETAIL.encode()),
    raises(httpx.ConnectError(SECRET_DETAIL)),
    raises(httpx.ReadError(SECRET_DETAIL)),
]


@pytest.mark.parametrize(
    "behavior",
    LEAKY_FAILURES,
    ids=["http_500", "http_429", "invalid_body", "connect_error", "read_error"],
)
async def test_provider_failures_do_not_leak_details(router, behavior):
    router.primary.behavior = behavior
    router.backup.behavior = behavior

    response = await router.post()

    assert response.status_code == 502
    assert_no_leaks(response)


async def test_timeout_failure_does_not_leak_details(router, monkeypatch):
    use_short_timeouts(monkeypatch)
    router.primary.behavior = hang
    router.backup.behavior = hang

    response = await router.post()

    assert_error(response, 504, "GATEWAY_TIMEOUT")
    assert_no_leaks(response)


async def test_successful_response_exposes_only_public_fields(router):
    router.primary.behavior = ok({"total_tokens": 5, "internal_note": SECRET})

    response = await router.post()

    assert set(response.json()) == {"text", "model", "provider", "tokens_charged"}
    assert_no_leaks(response)


async def test_provider_failure_logs_contain_no_secrets(router, caplog):
    router.primary.behavior = raises(httpx.ConnectError(SECRET_DETAIL))

    with caplog.at_level(logging.INFO, logger="model_router"):
        await router.post()

    assert "primary attempt: unavailable" in caplog.text
    assert SECRET not in caplog.text


async def test_tokenizer_failure_returns_internal_error(router, monkeypatch):
    def fail(text):
        raise RuntimeError(SECRET_DETAIL)

    monkeypatch.setattr(gateway, "count_tokens", fail)

    response = await router.post()

    assert_error(response, 500, "INTERNAL_ERROR")
    assert_no_leaks(response)
    assert router.provider_calls() == 0
    assert router.rows() == []


async def test_reservation_failure_returns_internal_error_without_provider_call(router, monkeypatch):
    async def fail(tenant_id, tokens):
        raise sqlite3.OperationalError(SECRET_DETAIL)

    monkeypatch.setattr(router.limiter, "reserve", fail)

    response = await router.post()

    assert_error(response, 500, "INTERNAL_ERROR")
    assert_no_leaks(response)
    assert router.provider_calls() == 0


async def test_database_error_during_reservation_recovers_on_next_request(router):
    with contextlib.closing(sqlite3.connect(router.db_path)) as connection:
        connection.execute(
            "CREATE TRIGGER block_insert BEFORE INSERT ON token_usage "
            "BEGIN SELECT RAISE(ABORT, 'insert blocked'); END"
        )
        connection.commit()

    response = await router.post()

    assert_error(response, 500, "INTERNAL_ERROR")
    assert router.provider_calls() == 0

    with contextlib.closing(sqlite3.connect(router.db_path)) as connection:
        connection.execute("DROP TRIGGER block_insert")
        connection.commit()

    assert (await router.post()).status_code == 200


async def test_unexpected_provider_error_returns_internal_error_and_keeps_reservation(router):
    router.primary.behavior = raises(RuntimeError(SECRET_DETAIL))

    response = await router.post()

    assert_error(response, 500, "INTERNAL_ERROR")
    assert_no_leaks(response)
    assert router.backup.calls == []
    assert router.rows() == [("tenant-a", RESERVED_TOKENS)]


@pytest.mark.parametrize(
    "error",
    [sqlite3.OperationalError("database is locked"), ValueError("no active connection")],
    ids=["sqlite_error", "closed_connection"],
)
async def test_settlement_failure_still_returns_completion(router, monkeypatch, error):
    async def fail(reservation_id, tokens):
        raise error

    monkeypatch.setattr(router.limiter, "settle", fail)
    router.primary.behavior = ok(total(INPUT_TOKENS + 1))

    response = await router.post()

    assert response.status_code == 200
    assert response.json()["tokens_charged"] == RESERVED_TOKENS
    assert router.rows() == [("tenant-a", RESERVED_TOKENS)]


async def test_settlement_failure_after_provider_rejection_keeps_reservation(router, monkeypatch):
    async def fail(reservation_id, tokens):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(router.limiter, "settle", fail)
    router.primary.behavior = status(500)

    response = await router.post()

    assert_error(response, 502, "PROVIDER_ERROR")
    assert router.rows() == [("tenant-a", RESERVED_TOKENS)]


async def test_concurrent_tenants_receive_independent_results(router, monkeypatch):
    fixed_input_tokens(monkeypatch, 9_000)
    router.primary.behavior = ok()
    payload = {"prompt": PROMPT, "max_tokens": 1_000}

    responses = await asyncio.gather(
        *[router.post(payload, KEY_A) for _ in range(8)],
        *[router.post(payload, KEY_B) for _ in range(8)],
    )

    tenant_a = sorted(response.status_code for response in responses[:8])
    tenant_b = sorted(response.status_code for response in responses[8:])
    assert tenant_a == [200] * 5 + [429] * 3
    assert tenant_b == [200] * 5 + [429] * 3
    assert router.rows().count(("tenant-a", 10_000)) == 5
    assert router.rows().count(("tenant-b", 10_000)) == 5
    assert router.provider_calls() == 10


async def test_concurrent_fallbacks_create_one_row_per_request(router):
    router.primary.behavior = status(429)
    router.backup.behavior = ok(total(RESERVED_TOKENS))

    responses = await asyncio.gather(*[router.post() for _ in range(20)])

    assert [response.json()["provider"] for response in responses] == ["backup"] * 20
    assert router.rows() == [("tenant-a", RESERVED_TOKENS)] * 20
    assert len(router.primary.calls) == 20
    assert len(router.backup.calls) == 20


async def wait_until_entered(provider):
    await asyncio.wait_for(provider.entered.wait(), EVENT_WAIT_SECONDS)


async def test_cancellation_during_primary_keeps_full_reservation(router, monkeypatch):
    settle_calls = count_calls(monkeypatch, router.limiter, "settle")
    router.primary.behavior = hang

    task = asyncio.create_task(router.post())
    await wait_until_entered(router.primary)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert router.primary.cancelled
    assert router.backup.calls == []
    assert settle_calls == []
    assert router.rows() == [("tenant-a", RESERVED_TOKENS)]


@pytest.mark.parametrize(
    "primary_behavior",
    [status(429), hang],
    ids=["primary_429", "primary_timeout"],
)
async def test_cancellation_during_backup_keeps_full_reservation(router, monkeypatch, primary_behavior):
    monkeypatch.setattr(gateway, "PRIMARY_TIMEOUT_SECONDS", SHORT_TIMEOUT)
    router.primary.behavior = primary_behavior
    router.backup.behavior = hang

    task = asyncio.create_task(router.post())
    await wait_until_entered(router.backup)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert router.backup.cancelled
    assert router.rows() == [("tenant-a", RESERVED_TOKENS)]


async def test_cancellation_while_settling_keeps_full_reservation(router):
    release = asyncio.Event()

    async def wait_then_respond(request):
        await release.wait()
        return await ok(total(INPUT_TOKENS + 1))(request)

    router.primary.behavior = wait_then_respond
    task = asyncio.create_task(router.post())
    await wait_until_entered(router.primary)
    await router.limiter._lock.acquire()
    release.set()
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    router.limiter._lock.release()
    assert router.rows() == [("tenant-a", RESERVED_TOKENS)]


async def test_cancelled_request_does_not_block_following_request(router):
    router.primary.behavior = hang
    task = asyncio.create_task(router.post())
    await wait_until_entered(router.primary)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    router.primary.behavior = ok(total(RESERVED_TOKENS))
    assert (await router.post()).status_code == 200


async def test_lifespan_opens_and_closes_resources(tmp_path, monkeypatch):
    db_path = tmp_path / "lifespan.db"
    monkeypatch.setattr(gateway, "DATABASE_PATH", str(db_path))

    async with gateway.lifespan(gateway.app):
        client = gateway.app.state.provider_client
        assert await gateway.app.state.limiter.reserve("tenant-a", 1) is not None
        assert not client.is_closed

    assert client.is_closed
    assert db_path.exists()
