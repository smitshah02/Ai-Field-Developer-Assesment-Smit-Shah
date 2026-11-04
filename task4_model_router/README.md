# LLM Model Router

A FastAPI LLM gateway with a per-tenant, token-aware sliding-window rate limiter (default 50,000 tokens per 60 seconds per API key, stored in on-disk SQLite) and primary-to-backup model failover.

## Requirements

- **Python >= 3.11**: `gateway.py` uses `asyncio.timeout`, which doesn't exist in 3.10.
- **Internet access on the first run**: tiktoken downloads the `cl100k_base` tokenizer on first import and caches it. On offline machines, point `TIKTOKEN_CACHE_DIR` at a pre-populated cache directory.

## Setup

Follow [Setup](../README.md#setup) in the root README, then `cd task4_model_router`.

## Project layout

```text
.
├── gateway.py          # Endpoint, failover, token settlement, errors
├── rate_limiter.py     # Token counting and SQLite sliding-window limiter
├── mock_providers.py   # Mock primary and backup providers
├── README.md
└── tests/
```

`rate_limiter.db` is created at runtime.

## Run

Use three terminals, all in this folder with the venv active:

```bash
uvicorn mock_providers:primary_app --port 9201
uvicorn mock_providers:backup_app --port 9202
uvicorn gateway:app --port 8000
```

Set `MOCK_PRIMARY_MODE` or `MOCK_BACKUP_MODE` to choose how a mock behaves: `success` (default), `rate_limit` (429), `fail` (500 with a fake traceback, used to show sanitization) or `timeout` (sleeps `MOCK_DELAY_SECONDS`, default 5). For example, restart the primary with:

```bash
MOCK_PRIMARY_MODE=timeout uvicorn mock_providers:primary_app --port 9201
```

## Configuration

| Variable | Default |
|---|---|
| `PRIMARY_URL` | `http://127.0.0.1:9201/v1/completions` |
| `BACKUP_URL` | `http://127.0.0.1:9202/v1/completions` |
| `PRIMARY_TIMEOUT_SECONDS` | `3.0` |
| `BACKUP_TIMEOUT_SECONDS` | `10.0` |
| `RATE_LIMIT_DB_PATH` | `rate_limiter.db` next to `gateway.py` |
| `TOKENS_PER_MINUTE` | `50000` |
| `TENANT_API_KEYS` | `demo-key-a:tenant-a,demo-key-b:tenant-b` (format `key:tenant,key:tenant`) |

## API

`POST /v1/completions` with header `x-api-key` and body `{"prompt": str, "max_tokens": 1–4096, default 256}`. A success looks like:

```json
{"text": "...", "model": "mock-primary-1", "provider": "primary", "tokens_charged": 14}
```

## How rate limiting works

- Before calling a provider, the gateway reserves the input tokens (counted with tiktoken) plus `max_tokens` in SQLite under `BEGIN IMMEDIATE`, so the limit holds across concurrent requests and processes.
- Rows older than 60 seconds are evicted on each reservation, making this a true sliding window, not a per-calendar-minute counter.
- After the call, the reservation is settled to the provider's reported `total_tokens`, never below the input tokens.
- When a provider may already have processed the request (timeout, broken connection, invalid body) or usage is missing, the full reservation is kept.
- A request is rejected with 429 if its reservation would exceed the limit.

## Failover rules

The backup is tried once on a primary 429, a primary timeout (3000 ms total, including a slow body), a refused connection, or a primary 502, 503 or 504.

There is no failover on other 4xx or 500 responses, on a connection broken mid-request, or on an invalid body, because those either would fail the same way on the backup or the primary may have already processed the request.

## Errors

Every error uses the shape `{"error": {"code", "message"}}` and never includes upstream bodies, tracebacks or hostnames.

| HTTP status | Code | Meaning |
|---|---|---|
| 400 | `INVALID_REQUEST` | Invalid request body |
| 401 | `INVALID_API_KEY` | Missing or unknown API key |
| 404 / 405 | `HTTP_ERROR` | Unknown route or wrong method |
| 429 | `RATE_LIMIT_EXCEEDED` | Tenant token limit would be exceeded |
| 500 | `INTERNAL_ERROR` | Unexpected gateway failure |
| 502 | `PROVIDER_UNAVAILABLE` | Primary connection broke mid-request |
| 502 | `PROVIDER_ERROR` | Primary returned another error status or an invalid body |
| 502 | `ALL_PROVIDERS_FAILED` | Primary and backup both failed |
| 504 | `GATEWAY_TIMEOUT` | The last provider tried timed out |

## Try it

```bash
BODY='{"prompt": "Say hello", "max_tokens": 50}'

curl -s localhost:8000/v1/completions -H "x-api-key: demo-key-a" -d "$BODY"

curl -s localhost:8000/v1/completions -H "x-api-key: wrong" -d "$BODY"
```

The first request succeeds and the second returns 401 `INVALID_API_KEY`. Restart the primary with `MOCK_PRIMARY_MODE=rate_limit` and repeat the first request to see `"provider": "backup"`.

## Tests

```bash
pytest -v tests/
```

The tests use temporary databases and in-process fakes, so no servers are needed.
