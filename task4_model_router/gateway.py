import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from rate_limiter import DEFAULT_TOKEN_LIMIT, TokenRateLimiter, count_tokens

PRIMARY_URL = os.environ.get("PRIMARY_URL", "http://127.0.0.1:9201/v1/completions")
BACKUP_URL = os.environ.get("BACKUP_URL", "http://127.0.0.1:9202/v1/completions")
PRIMARY_TIMEOUT_SECONDS = float(os.environ.get("PRIMARY_TIMEOUT_SECONDS", "3.0"))
BACKUP_TIMEOUT_SECONDS = float(os.environ.get("BACKUP_TIMEOUT_SECONDS", "10.0"))

DATABASE_PATH = os.environ.get(
    "RATE_LIMIT_DB_PATH", str(Path(__file__).with_name("rate_limiter.db"))
)
TOKEN_LIMIT = int(os.environ.get("TOKENS_PER_MINUTE", DEFAULT_TOKEN_LIMIT))
TENANT_API_KEYS = dict(
    entry.split(":", 1)
    for entry in os.environ.get(
        "TENANT_API_KEYS", "demo-key-a:tenant-a,demo-key-b:tenant-b"
    ).split(",")
)

MAX_PROMPT_CHARS = 400_000
MAX_OUTPUT_TOKENS = 4_096
DEFAULT_OUTPUT_TOKENS = 256
MAX_REPORTED_TOKENS = 1_000_000

SUCCESS = "success"
RATE_LIMITED = "rate_limited"
TIMED_OUT = "timed_out"
UNAVAILABLE = "unavailable"
FAILED = "failed"

logger = logging.getLogger("model_router")


class CompletionRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    max_tokens: int = Field(default=DEFAULT_OUTPUT_TOKENS, ge=1, le=MAX_OUTPUT_TOKENS)


class ProviderCompletion(BaseModel):
    text: str
    model: str
    usage: Any = None


@dataclass
class Attempt:
    outcome: str
    maybe_processed: bool
    completion: ProviderCompletion | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    limiter = TokenRateLimiter(DATABASE_PATH, TOKEN_LIMIT)
    await limiter.open()
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            app.state.limiter = limiter
            app.state.provider_client = client
            yield
    finally:
        await limiter.close()


app = FastAPI(title="LLM Model Router", lifespan=lifespan)


def gateway_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status_code
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, error: StarletteHTTPException) -> JSONResponse:
    return gateway_error(error.status_code, "HTTP_ERROR", "Request could not be processed")


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, error: Exception) -> JSONResponse:
    logger.error("unexpected gateway failure: %s", type(error).__name__)
    return gateway_error(500, "INTERNAL_ERROR", "Gateway failed to process the request")


async def attempt_provider(
    client: httpx.AsyncClient, url: str, payload: dict, timeout_seconds: float
) -> Attempt:
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await client.post(url, json=payload)
    except (TimeoutError, httpx.TimeoutException):
        return Attempt(TIMED_OUT, maybe_processed=True)
    except httpx.ConnectError:
        return Attempt(UNAVAILABLE, maybe_processed=False)
    except httpx.HTTPError:
        return Attempt(UNAVAILABLE, maybe_processed=True)

    if response.status_code == 429:
        return Attempt(RATE_LIMITED, maybe_processed=False)
    if response.status_code in (502, 503, 504):
        return Attempt(UNAVAILABLE, maybe_processed=False)
    if response.status_code != 200:
        return Attempt(FAILED, maybe_processed=False)
    try:
        completion = ProviderCompletion.model_validate_json(response.content)
    except ValidationError:
        return Attempt(FAILED, maybe_processed=True)
    return Attempt(SUCCESS, maybe_processed=True, completion=completion)


def should_fail_over(attempt: Attempt) -> bool:
    if attempt.outcome in (RATE_LIMITED, TIMED_OUT):
        return True
    return attempt.outcome == UNAVAILABLE and not attempt.maybe_processed


def reported_tokens(usage: Any) -> int | None:
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    if isinstance(total, int) and not isinstance(total, bool) and 0 < total <= MAX_REPORTED_TOKENS:
        return total
    return None


def tokens_to_charge(input_tokens: int, reserved_tokens: int, attempts: list[Attempt]) -> int:
    final_attempt = attempts[-1]
    if final_attempt.completion is None:
        uncertain = any(attempt.maybe_processed for attempt in attempts)
        return reserved_tokens if uncertain else 0

    reported = reported_tokens(final_attempt.completion.usage)
    if reported is None:
        return reserved_tokens
    if any(attempt.maybe_processed for attempt in attempts[:-1]):
        return max(reserved_tokens, reported)
    return max(input_tokens, reported)


def provider_failure_response(attempts: list[Attempt]) -> JSONResponse:
    final_outcome = attempts[-1].outcome
    if final_outcome == TIMED_OUT:
        return gateway_error(504, "GATEWAY_TIMEOUT", "Model provider timed out")
    if len(attempts) > 1:
        return gateway_error(502, "ALL_PROVIDERS_FAILED", "All model providers failed")
    if final_outcome == UNAVAILABLE:
        return gateway_error(502, "PROVIDER_UNAVAILABLE", "Model provider is unavailable")
    return gateway_error(502, "PROVIDER_ERROR", "Model provider returned an invalid response")


async def settle_usage(
    limiter: TokenRateLimiter, reservation_id: int, tokens: int, reserved_tokens: int
) -> int:
    try:
        await limiter.settle(reservation_id, tokens)
    except (aiosqlite.Error, ValueError):
        logger.error("token reconciliation failed; keeping full reservation")
        return reserved_tokens
    return tokens


@app.post("/v1/completions")
async def completions(request: Request) -> JSONResponse:
    tenant_id = TENANT_API_KEYS.get(request.headers.get("x-api-key"))
    if tenant_id is None:
        return gateway_error(401, "INVALID_API_KEY", "Missing or invalid API key")

    try:
        body = CompletionRequest.model_validate_json(await request.body())
    except ValidationError:
        return gateway_error(400, "INVALID_REQUEST", "Invalid completion request")

    limiter = request.app.state.limiter
    client = request.app.state.provider_client
    input_tokens = await asyncio.to_thread(count_tokens, body.prompt)
    reserved_tokens = input_tokens + body.max_tokens
    reservation_id = await limiter.reserve(tenant_id, reserved_tokens)
    if reservation_id is None:
        return gateway_error(429, "RATE_LIMIT_EXCEEDED", "Tenant token limit exceeded")

    payload = body.model_dump()
    provider_name = "primary"
    attempts = [await attempt_provider(client, PRIMARY_URL, payload, PRIMARY_TIMEOUT_SECONDS)]
    logger.info("primary attempt: %s", attempts[0].outcome)
    if should_fail_over(attempts[0]):
        provider_name = "backup"
        attempts.append(await attempt_provider(client, BACKUP_URL, payload, BACKUP_TIMEOUT_SECONDS))
        logger.info("backup attempt: %s", attempts[1].outcome)

    charged_tokens = await settle_usage(
        limiter,
        reservation_id,
        tokens_to_charge(input_tokens, reserved_tokens, attempts),
        reserved_tokens,
    )

    completion = attempts[-1].completion
    if completion is None:
        return provider_failure_response(attempts)
    return JSONResponse(
        {
            "text": completion.text,
            "model": completion.model,
            "provider": provider_name,
            "tokens_charged": charged_tokens,
        }
    )
