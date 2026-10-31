import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway
import mock_provider
from redactor import REDACTED as R

pytestmark = pytest.mark.asyncio

MAX_EVENT = gateway.MAX_EVENT_BYTES
DONE = b"data: [DONE]\n\n"
UNREDACTED_VALUES = ("john", "4111", "123-45", "@example")
TIMEOUT_SECONDS = 5


def delta(text) -> bytes:
    body = json.dumps({"choices": [{"text": text}]}, ensure_ascii=False)
    return f"data: {body}\n\n".encode()


def event_of_size(total_bytes: int) -> bytes:
    prefix = b'data: {"choices":[{"text":"'
    suffix = b'"}]}'
    filler_length = total_bytes - len(prefix) - len(suffix)
    filler = (b"word " * (filler_length // 5 + 1))[:filler_length]
    return prefix + filler + suffix


def parse_client_stream(body: str) -> tuple[str, str | None, list[str]]:
    assert body == "" or body.endswith("\n\n")
    text = []
    errors = []
    terminator = None
    for frame in body.split("\n\n")[:-1]:
        assert terminator is None
        if frame == "data: [DONE]":
            terminator = "done"
        elif frame.startswith("event: error\ndata: "):
            errors.append(json.loads(frame.removeprefix("event: error\ndata: "))["error"])
            terminator = "error"
        else:
            assert frame.startswith("data: ")
            text.append(json.loads(frame.removeprefix("data: "))["text"])
    return "".join(text), terminator, errors


class ScriptedStream(httpx.AsyncByteStream):
    def __init__(self, steps):
        self.steps = steps
        self.chunks_sent = 0
        self.close_count = 0
        self.exhausted = False

    async def __aiter__(self):
        for step in self.steps:
            if isinstance(step, Exception):
                raise step
            if isinstance(step, asyncio.Event):
                await step.wait()
                continue
            self.chunks_sent += 1
            yield step
        self.exhausted = True

    async def aclose(self):
        self.close_count += 1


class FakeProvider:
    def __init__(self):
        self.steps = []
        self.status_code = 200
        self.content_type = "text/event-stream"
        self.connect_error = None
        self.requests = []
        self.stream = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.connect_error:
            raise self.connect_error
        self.stream = ScriptedStream(self.steps)
        headers = {"content-type": self.content_type} if self.content_type else {}
        return httpx.Response(self.status_code, headers=headers, stream=self.stream)


@pytest.fixture
def provider():
    fake = FakeProvider()
    gateway.app.state.provider_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    yield fake
    del gateway.app.state.provider_client


async def post(payload=None) -> httpx.Response:
    transport = httpx.ASGITransport(app=gateway.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        return await client.post("/v1/generate", json={"prompt": "hi"} if payload is None else payload)


class AsgiClient:
    def __init__(self, spec_version="2.3", failing_send_after=None):
        self.spec_version = spec_version
        self.failing_send_after = failing_send_after
        self.messages = []
        self.first_body = asyncio.Event()
        self.disconnect = asyncio.Event()
        self.body_read = False

    async def receive(self):
        if not self.body_read:
            self.body_read = True
            return {"type": "http.request", "body": b'{"prompt": "hi"}', "more_body": False}
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        if self.failing_send_after is not None and len(self.messages) >= self.failing_send_after:
            raise OSError("client went away")
        self.messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            self.first_body.set()

    def body(self) -> str:
        return b"".join(
            message.get("body", b"") for message in self.messages if message["type"] == "http.response.body"
        ).decode()

    def start(self) -> asyncio.Task:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": self.spec_version},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/generate",
            "raw_path": b"/v1/generate",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        }
        return asyncio.create_task(gateway.app(scope, self.receive, self.send))


async def finish(task: asyncio.Task) -> None:
    done, _ = await asyncio.wait({task}, timeout=TIMEOUT_SECONDS)
    assert done, "gateway did not finish"
    if not task.cancelled():
        task.exception()


async def test_successful_stream_is_sanitized_and_ends_with_done(provider):
    provider.steps = [delta("Contact john@"), delta("example"), delta(".com for details"), DONE]
    response = await post()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert parse_client_stream(response.text) == (f"Contact {R} for details", "done", [])
    assert provider.stream.close_count == 1


async def test_request_is_forwarded_to_the_provider(provider):
    provider.steps = [DONE]
    await post({"prompt": "tell me a story"})
    request = provider.requests[0]
    assert str(request.url) == gateway.PROVIDER_URL
    assert request.headers["accept"] == "text/event-stream"
    assert json.loads(request.content) == {"prompt": "tell me a story", "stream": True}


@pytest.mark.parametrize("payload", [{}, {"prompt": 5}, {"text": "hi"}])
async def test_invalid_request_is_rejected_without_calling_the_provider(provider, payload):
    response = await post(payload)
    assert response.status_code == 422
    assert provider.requests == []


async def test_all_pii_types_are_redacted_in_client_stream(provider):
    provider.steps = [
        delta("Mail john.d"),
        delta("oe@exa"),
        delta("mple.com, SSN 123-4"),
        delta("5-6789, card 4111 11"),
        delta("11 1111 1111 and anna@example.org"),
        DONE,
    ]
    response = await post()
    text, terminator, _ = parse_client_stream(response.text)
    assert text == f"Mail {R}, SSN {R}, card {R} and {R}"
    assert terminator == "done"
    assert not any(value in response.text for value in UNREDACTED_VALUES)


async def test_value_ending_at_the_final_delta_is_flushed_redacted(provider):
    provider.steps = [delta("card 4111 1111 "), delta("1111 1111"), DONE]
    response = await post()
    assert parse_client_stream(response.text) == (f"card {R}", "done", [])


async def test_incomplete_value_at_done_is_redacted(provider):
    provider.steps = [delta("reach me at john@exam"), DONE]
    response = await post()
    assert parse_client_stream(response.text) == (f"reach me at {R}", "done", [])


async def test_sse_events_split_at_every_byte_boundary(provider):
    body = delta("café ☕ john@") + delta("example.com done ") + DONE
    for cut in range(1, len(body)):
        provider.steps = [body[:cut], body[cut:]]
        response = await post()
        assert parse_client_stream(response.text) == (f"café ☕ {R} done ", "done", [])


async def test_sse_stream_delivered_one_byte_at_a_time(provider):
    body = delta("日本語 123-45-") + delta("6789 ok") + DONE
    provider.steps = [body[index:index + 1] for index in range(len(body))]
    response = await post()
    assert parse_client_stream(response.text) == (f"日本語 {R} ok", "done", [])


async def test_multiple_sse_events_in_one_http_chunk(provider):
    provider.steps = [delta("one ") + delta("two ") + delta("three ") + DONE]
    response = await post()
    assert parse_client_stream(response.text) == ("one two three ", "done", [])


async def test_crlf_framing_split_across_chunks(provider):
    body = (delta("café ") + DONE).replace(b"\n", b"\r\n")
    for cut in range(1, len(body)):
        provider.steps = [body[:cut], body[cut:]]
        response = await post()
        assert parse_client_stream(response.text) == ("café ", "done", [])


async def test_non_data_fields_and_comments_are_ignored(provider):
    provider.steps = [
        b": keep-alive\n\n",
        b"event: message\nid: 7\n" + delta("hello "),
        b"event: ping\n\n",
        DONE,
    ]
    response = await post()
    assert parse_client_stream(response.text) == ("hello ", "done", [])


async def test_multi_line_data_is_joined(provider):
    provider.steps = [b'data: {"choices":\ndata: [{"text": "joined "}]}\n\n', DONE]
    response = await post()
    assert parse_client_stream(response.text) == ("joined ", "done", [])


async def test_done_without_space_after_field_name(provider):
    provider.steps = [delta("hi "), b"data:[DONE]\n\n"]
    response = await post()
    assert parse_client_stream(response.text) == ("hi ", "done", [])


async def test_events_after_done_are_not_forwarded(provider):
    provider.steps = [delta("hi "), DONE + delta("john@example.com ")]
    response = await post()
    assert parse_client_stream(response.text) == ("hi ", "done", [])


async def test_metadata_and_empty_deltas_produce_no_client_text(provider):
    provider.steps = [
        b'data: {"choices": [], "model": "mock"}\n\n',
        delta(""),
        b'data: {"choices": [{"text": "", "finish_reason": "stop"}]}\n\n',
        DONE,
    ]
    response = await post()
    assert response.text == "data: [DONE]\n\n"


@pytest.mark.parametrize(
    "failing_steps",
    [
        [],
        [b"data: {not json\n\n"],
        [b"data: \xff\xfe\n\n"],
        [delta(None)],
        [delta(5)],
        [b'data: {"text": "no choices"}\n\n'],
        [b'data: {"choices": [{}]}\n\n'],
        [b'data: {"choices": [1]}\n\n'],
        [b'data: {"choices": "abc"}\n\n'],
        [b"data: []\n\n"],
        [b"data: null\n\n"],
        [b'data: {"error": "provider overloaded"}\n\n'],
        [event_of_size(MAX_EVENT + 1) + b"\n\n"],
        [event_of_size(MAX_EVENT + 1)],
        [b"data: partial event without end"],
        [httpx.RemoteProtocolError("peer closed connection john@example.com")],
        [httpx.ReadError("connection reset")],
        [httpx.ReadTimeout("slow")],
        [ValueError("unexpected")],
    ],
    ids=[
        "missing_done",
        "invalid_json",
        "invalid_utf8",
        "null_text",
        "numeric_text",
        "missing_choices",
        "missing_text",
        "choice_not_object",
        "choices_not_list",
        "event_is_list",
        "event_is_null",
        "provider_error_event",
        "oversized_complete_event",
        "oversized_incomplete_event",
        "cut_off_event",
        "protocol_error",
        "read_error",
        "read_timeout",
        "unexpected_exception",
    ],
)
async def test_mid_stream_failures_end_with_a_safe_error_and_discard_held_text(provider, caplog, failing_steps):
    provider.steps = [delta("safe john@exam"), *failing_steps]
    response = await post()
    text, terminator, errors = parse_client_stream(response.text)
    assert response.status_code == 200
    assert text == "safe "
    assert terminator == "error"
    assert errors[0] in {"Provider stream failed", "Provider stream ended unexpectedly", "Provider timed out"}
    assert not any(value in response.text for value in UNREDACTED_VALUES)
    assert "Traceback" not in response.text
    assert not any(value in caplog.text for value in UNREDACTED_VALUES)
    assert provider.stream.close_count == 1


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (httpx.ReadTimeout("slow"), "Provider timed out"),
        (httpx.RemoteProtocolError("internal detail 10.0.0.5"), "Provider stream failed"),
        (RuntimeError("internal detail 10.0.0.5"), "Provider stream failed"),
    ],
)
async def test_error_events_do_not_expose_internal_details(provider, failure, message):
    provider.steps = [failure]
    response = await post()
    assert parse_client_stream(response.text) == ("", "error", [message])
    assert "10.0.0.5" not in response.text


async def test_stream_ending_without_done_reports_unexpected_end(provider):
    provider.steps = [delta("hello ")]
    response = await post()
    assert parse_client_stream(response.text) == (
        "hello ",
        "error",
        ["Provider stream ended unexpectedly"],
    )


async def test_failure_after_complete_value_keeps_it_redacted(provider):
    provider.steps = [delta("mail john@example.com "), delta("more 4111 1111 1111 1111"), b"data: {bad\n\n"]
    response = await post()
    text, terminator, _ = parse_client_stream(response.text)
    assert text == f"mail {R} more "
    assert terminator == "error"


async def test_complete_event_at_exact_size_limit_is_accepted(provider):
    event = event_of_size(MAX_EVENT)
    assert len(event) == MAX_EVENT
    provider.steps = [event + b"\n\n", DONE]
    response = await post()
    text, terminator, _ = parse_client_stream(response.text)
    assert terminator == "done"
    assert len(text) == MAX_EVENT - len(b'data: {"choices":[{"text":"') - len(b'"}]}')


async def test_incomplete_event_at_exact_size_limit_is_accepted(provider):
    provider.steps = [event_of_size(MAX_EVENT), b"\n\n", DONE]
    response = await post()
    assert parse_client_stream(response.text)[1] == "done"


SEPARATOR_DELIVERIES = [
    [b"\n\n"],
    [b"\n", b"\n"],
    [b"\r\n\r\n"],
    [b"\r", b"\n\r\n"],
    [b"\r\n", b"\r\n"],
    [b"\r\n\r", b"\n"],
    [b"\r", b"\n", b"\r", b"\n"],
]


def with_separator(event: bytes, separator_chunks: list[bytes]) -> list[bytes]:
    first_in_event_chunk = [event + separator_chunks[0], *separator_chunks[1:]]
    first_in_own_chunk = [event, *separator_chunks]
    return [first_in_event_chunk, first_in_own_chunk]


@pytest.mark.parametrize("separator_chunks", SEPARATOR_DELIVERIES)
@pytest.mark.parametrize("delivery", [0, 1])
async def test_event_of_exactly_65536_bytes_is_accepted_for_any_separator_split(
    provider, separator_chunks, delivery
):
    event = event_of_size(65536)
    provider.steps = [*with_separator(event, separator_chunks)[delivery], DONE]
    response = await post()
    text, terminator, errors = parse_client_stream(response.text)
    assert (terminator, errors) == ("done", [])
    assert len(text) == 65536 - len(b'data: {"choices":[{"text":"') - len(b'"}]}')


@pytest.mark.parametrize("separator_chunks", SEPARATOR_DELIVERIES)
@pytest.mark.parametrize("delivery", [0, 1])
async def test_event_of_65537_bytes_is_rejected_for_any_separator_split(
    provider, separator_chunks, delivery
):
    provider.steps = [*with_separator(event_of_size(65537), separator_chunks)[delivery], DONE]
    response = await post()
    assert parse_client_stream(response.text) == ("", "error", ["Provider stream failed"])


@pytest.mark.parametrize("trailing", [b"\r\r", b"\n\r\r", b"\nx", b"\rx"])
async def test_trailing_bytes_beyond_a_partial_separator_count_towards_the_limit(provider, trailing):
    provider.steps = [event_of_size(65536) + trailing, b"never read", DONE]
    response = await post()
    assert parse_client_stream(response.text) == ("", "error", ["Provider stream failed"])
    assert provider.stream.chunks_sent == 1


async def test_complete_event_one_byte_over_the_limit_is_rejected(provider):
    provider.steps = [event_of_size(MAX_EVENT + 1) + b"\n\n" + DONE]
    response = await post()
    assert parse_client_stream(response.text) == ("", "error", ["Provider stream failed"])


async def test_incomplete_event_one_byte_over_the_limit_is_rejected(provider):
    provider.steps = [event_of_size(MAX_EVENT + 1), b"\n\n", DONE]
    response = await post()
    assert parse_client_stream(response.text) == ("", "error", ["Provider stream failed"])


async def test_event_growing_over_the_limit_across_chunks_stops_reading(provider):
    provider.steps = [b"data: " + b"x" * 40000, b"x" * 40000, b"never read", DONE]
    response = await post()
    assert parse_client_stream(response.text) == ("", "error", ["Provider stream failed"])
    assert provider.stream.chunks_sent == 2


async def test_oversized_event_rejects_its_chunk_before_any_event_is_parsed(provider):
    provider.steps = [delta("hello ") + event_of_size(MAX_EVENT + 1) + b"\n\n" + delta("late ") + DONE]
    response = await post()
    assert parse_client_stream(response.text) == ("", "error", ["Provider stream failed"])


@pytest.mark.parametrize(
    "content_type",
    [
        "text/event-stream",
        "text/event-stream; charset=utf-8",
        "text/event-stream;charset=UTF-8",
        "Text/Event-Stream",
        "TEXT/EVENT-STREAM ; charset=utf-8",
    ],
)
async def test_event_stream_content_types_are_accepted(provider, content_type):
    provider.content_type = content_type
    provider.steps = [delta("hello "), DONE]
    response = await post()
    assert response.status_code == 200
    assert parse_client_stream(response.text) == ("hello ", "done", [])


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "text/plain", "text/event-stream-extra", "text/event", None],
)
async def test_wrong_content_type_is_a_bad_gateway(provider, content_type):
    provider.content_type = content_type
    provider.steps = [b'{"text": "john@example.com"}']
    response = await post()
    assert response.status_code == 502
    assert response.json() == {"error": "Provider returned an invalid response"}
    assert provider.stream.close_count == 1


@pytest.mark.parametrize("status_code", [201, 204, 400, 401, 404, 429, 500, 503])
async def test_provider_http_errors_are_a_bad_gateway(provider, status_code):
    provider.status_code = status_code
    provider.steps = [b"internal detail john@example.com"]
    response = await post()
    assert response.status_code == 502
    assert response.json() == {"error": "Provider returned an invalid response"}
    assert provider.stream.close_count == 1


@pytest.mark.parametrize(
    ("error", "status_code", "message"),
    [
        (httpx.ConnectError("refused 10.0.0.5"), 502, "Provider unavailable"),
        (httpx.RemoteProtocolError("bad 10.0.0.5"), 502, "Provider unavailable"),
        (httpx.ReadError("reset 10.0.0.5"), 502, "Provider unavailable"),
        (httpx.ConnectTimeout("slow 10.0.0.5"), 504, "Provider timed out"),
        (httpx.ReadTimeout("slow 10.0.0.5"), 504, "Provider timed out"),
        (httpx.PoolTimeout("slow 10.0.0.5"), 504, "Provider timed out"),
    ],
)
async def test_provider_connection_failures_return_sanitized_errors(provider, error, status_code, message):
    provider.connect_error = error
    response = await post()
    assert response.status_code == status_code
    assert response.json() == {"error": message}
    assert "10.0.0.5" not in response.text


async def test_output_reaches_the_client_before_the_provider_finishes(provider):
    release = asyncio.Event()
    provider.steps = [delta("Hello world "), release, delta("goodbye "), DONE]
    client = AsgiClient()
    task = client.start()

    await asyncio.wait_for(client.first_body.wait(), TIMEOUT_SECONDS)

    assert parse_client_stream(client.body()) == ("Hello world ", None, [])
    assert provider.stream.chunks_sent == 1
    assert not provider.stream.exhausted
    assert not task.done()

    release.set()
    await finish(task)
    assert parse_client_stream(client.body()) == ("Hello world goodbye ", "done", [])


async def test_held_pii_is_not_visible_while_the_provider_is_stalled(provider):
    release = asyncio.Event()
    provider.steps = [delta("Contact john@exa"), release, delta("mple.com now"), DONE]
    client = AsgiClient()
    task = client.start()

    await asyncio.wait_for(client.first_body.wait(), TIMEOUT_SECONDS)
    assert client.body() == 'data: {"text": "Contact "}\n\n'

    release.set()
    await finish(task)
    assert parse_client_stream(client.body()) == (f"Contact {R} now", "done", [])


async def test_each_chunk_is_forwarded_before_the_next_is_read():
    stream = ScriptedStream([delta("one "), delta("two "), delta("three "), DONE])
    response = httpx.Response(200, stream=stream)
    reads_at_each_output = []
    async for event in gateway.sanitized_events(response):
        reads_at_each_output.append((event, stream.chunks_sent))
    assert reads_at_each_output == [
        (gateway.text_event("one "), 1),
        (gateway.text_event("two "), 2),
        (gateway.text_event("three "), 3),
        (gateway.DONE_EVENT, 4),
    ]


async def test_first_output_is_produced_from_the_first_content_chunk():
    stream = ScriptedStream([b'data: {"choices": []}\n\n', delta("Hi "), delta("never read"), DONE])
    events = gateway.sanitized_events(httpx.Response(200, stream=stream))
    assert await anext(events) == gateway.text_event("Hi ")
    assert stream.chunks_sent == 2
    await events.aclose()


async def test_client_disconnect_while_provider_is_stalled_closes_upstream(provider):
    provider.steps = [delta("Hello "), asyncio.Event(), DONE]
    client = AsgiClient(spec_version="2.3")
    task = client.start()

    await asyncio.wait_for(client.first_body.wait(), TIMEOUT_SECONDS)
    client.disconnect.set()
    await finish(task)

    assert provider.stream.close_count == 1
    assert not provider.stream.exhausted


async def test_client_disconnect_detected_on_send_closes_upstream(provider):
    provider.steps = [delta("one "), delta("two "), delta("three "), delta("four "), DONE]
    client = AsgiClient(spec_version="2.4", failing_send_after=2)
    task = client.start()
    await finish(task)

    assert provider.stream.close_count == 1
    assert provider.stream.chunks_sent < len(provider.steps)
    assert not provider.stream.exhausted


async def test_client_gone_before_response_starts_closes_upstream(provider):
    provider.steps = [delta("one "), DONE]
    client = AsgiClient(spec_version="2.4", failing_send_after=0)
    task = client.start()
    await finish(task)

    assert provider.stream.close_count == 1
    assert provider.stream.chunks_sent == 0


async def test_server_task_cancellation_closes_upstream(provider):
    provider.steps = [delta("Hello "), asyncio.Event(), DONE]
    client = AsgiClient(spec_version="2.4")
    task = client.start()

    await asyncio.wait_for(client.first_body.wait(), TIMEOUT_SECONDS)
    task.cancel()
    await finish(task)

    assert task.cancelled()
    assert provider.stream.close_count == 1


async def test_lifespan_creates_and_closes_the_provider_client():
    async with gateway.lifespan(gateway.app):
        client = gateway.app.state.provider_client
        assert not client.is_closed
        assert client.timeout == gateway.PROVIDER_TIMEOUT
    assert client.is_closed
    del gateway.app.state.provider_client


@pytest.fixture
def mock_provider_client(monkeypatch):
    monkeypatch.setattr(mock_provider, "CHUNK_DELAY_SECONDS", 0)
    gateway.app.state.provider_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_provider.app),
        base_url="http://provider",
    )
    yield
    del gateway.app.state.provider_client


async def test_end_to_end_with_the_mock_provider(mock_provider_client):
    response = await post()
    text, terminator, errors = parse_client_stream(response.text)
    assert terminator == "done"
    assert errors == []
    assert text == (
        "Here are the customer details for the cafe account.\n"
        f"Contact {R} for support. "
        f"SSN on file: {R}. "
        f"Card on file: {R} expires soon. "
        "Order number 1234 5678 stays visible. "
        f"Escalations go to {R}"
    )


async def test_mock_provider_splits_every_sample_value_across_deltas(monkeypatch):
    monkeypatch.setattr(mock_provider, "CHUNK_DELAY_SECONDS", 0)
    frames = [frame async for frame in mock_provider.completion_events()]
    raw = b"".join(frames)
    assert any(not frame.endswith(b"\n\n") for frame in frames)
    assert raw.endswith(DONE)
    joined = "".join(mock_provider.SAMPLE_CHUNKS)
    for value in ("john.doe@example.com", "123-45-6789", "4111 1111 1111 1111", "anna@example.org"):
        assert value in joined
        assert not any(value in chunk for chunk in mock_provider.SAMPLE_CHUNKS)
    assert joined.endswith("anna@example.org")


async def test_warning_is_logged_without_exception_message(provider, caplog):
    caplog.set_level(logging.WARNING, logger="stream_guardrail")
    provider.steps = [RuntimeError("john@example.com")]
    await post()
    assert "RuntimeError" in caplog.text
    assert "john@example.com" not in caplog.text
