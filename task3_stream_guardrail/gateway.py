import json
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from redactor import StreamRedactor

PROVIDER_URL = os.environ.get("PROVIDER_URL", "http://127.0.0.1:9100/v1/completions")
PROVIDER_TIMEOUT = httpx.Timeout(10.0, read=30.0)
MAX_EVENT_BYTES = 64 * 1024

DONE_EVENT = "data: [DONE]\n\n"

logger = logging.getLogger("stream_guardrail")


class GenerateRequest(BaseModel):
    prompt: str


class ProviderStreamError(Exception):
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(timeout=PROVIDER_TIMEOUT) as client:
        app.state.provider_client = client
        yield


app = FastAPI(title="LLM Streaming Guardrail Gateway", lifespan=lifespan)


def text_event(text: str) -> str:
    return f"data: {json.dumps({'text': text})}\n\n"


def error_event(message: str) -> str:
    return f"event: error\ndata: {json.dumps({'error': message})}\n\n"


def event_data(raw_event: bytes) -> str:
    lines = raw_event.decode("utf-8").split("\n")
    return "\n".join(
        line.removeprefix("data:").removeprefix(" ")
        for line in lines
        if line.startswith("data:")
    )


def delta_text(data: str) -> str:
    choices = json.loads(data)["choices"]
    return choices[0]["text"] if choices else ""


async def provider_events(provider_response: httpx.Response):
    pending = b""
    async for chunk in provider_response.aiter_bytes():
        pending = (pending + chunk).replace(b"\r\n", b"\n")
        *raw_events, pending = pending.split(b"\n\n")
        for raw_event in raw_events:
            data = event_data(raw_event)
            if data:
                yield data
        if len(pending) > MAX_EVENT_BYTES:
            raise ProviderStreamError("provider event exceeds size limit")


async def sanitized_events(provider_response: httpx.Response):
    redactor = StreamRedactor()
    try:
        async for data in provider_events(provider_response):
            if data == "[DONE]":
                remaining = redactor.flush()
                if remaining:
                    yield text_event(remaining)
                yield DONE_EVENT
                return
            safe_text = redactor.feed(delta_text(data))
            if safe_text:
                yield text_event(safe_text)
        logger.warning("provider stream ended without a completion event")
        yield error_event("Provider stream ended unexpectedly")
    except httpx.TimeoutException:
        logger.warning("provider stream timed out")
        yield error_event("Provider timed out")
    except Exception as error:
        logger.warning("provider stream failed: %s", type(error).__name__)
        yield error_event("Provider stream failed")


class SanitizedStreamResponse(StreamingResponse):
    def __init__(self, provider_response: httpx.Response) -> None:
        super().__init__(
            sanitized_events(provider_response),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self.provider_response = provider_response

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.provider_response.aclose()


def gateway_error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


@app.post("/v1/generate")
async def generate(body: GenerateRequest, request: Request) -> Response:
    client = request.app.state.provider_client
    provider_request = client.build_request(
        "POST",
        PROVIDER_URL,
        json={"prompt": body.prompt, "stream": True},
        headers={"Accept": "text/event-stream"},
    )
    try:
        provider_response = await client.send(provider_request, stream=True)
    except httpx.TimeoutException:
        return gateway_error(504, "Provider timed out")
    except httpx.HTTPError:
        return gateway_error(502, "Provider unavailable")

    content_type = provider_response.headers.get("content-type", "")
    if provider_response.status_code != 200 or not content_type.startswith("text/event-stream"):
        await provider_response.aclose()
        return gateway_error(502, "Provider returned an invalid response")

    return SanitizedStreamResponse(provider_response)
