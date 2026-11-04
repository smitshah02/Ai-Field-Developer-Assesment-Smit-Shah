# MCP Security Gateway

A FastAPI reverse proxy that sits between an AI client and a downstream MCP server. It checks a Bearer token, maps it to a role, and blocks `admin_*` tool calls for anyone who isn't an admin. Allowed requests are forwarded with httpx, and the downstream response is returned unchanged.

## Requirements

Python >= 3.10 (the shared repo venv needs 3.11 because of Task 4)

## Project layout

```text
.
├── gateway.py          # JSON-RPC parsing, auth checks, forwarding
├── auth.py             # Token-to-role mapping and the admin_* rule
├── mock_server.py      # Mock downstream MCP server
├── README.md
└── tests/
    └── test_gateway.py
```

## Setup

Follow [Setup](../README.md#setup) in the root README, then `cd task2_mcp_gateway`.

## Run

Use two terminals, both in the task folder with the venv active:

```bash
uvicorn mock_server:app --port 9000
uvicorn gateway:app --port 8000
```

`DOWNSTREAM_URL` defaults to `http://127.0.0.1:9000/mcp`. Set that environment variable to use a different downstream server.

## Demo tokens

| Token | Role |
|-------|------|
| `viewer-token` | viewer |
| `admin-token` | admin |

These are hardcoded for this assessment. This is not production auth: there is no JWT verification.

## Authorization rules

- Every request needs a valid Bearer token. Malformed requests are rejected with a 400 before the token is checked.
- `tools/list` is forwarded unchanged.
- `tools/call` to an `admin_*` name (case-insensitive) requires the admin role.
- The `Authorization` header is never forwarded downstream.

## Error codes

| Code | HTTP | Meaning |
|------|------|---------|
| -32700 | 400 | Parse error |
| -32600 | 400 | Invalid request, including batches, duplicate JSON keys, and non-notification messages without an id |
| -32602 | 200 | Missing or invalid `params.name` |
| -32001 | 200 | Unauthorized Tool Call, never forwarded |
| -32002 | 401 | Unauthenticated |
| -32003 | 502 / 504 | Downstream unavailable / timed out |

-32001 is required by this assessment. It is not a universal MCP standard code.

## Tests

```bash
pytest -v tests/
```

## Examples

```bash
# Viewer lists tools: forwarded
curl -s localhost:8000/mcp -H 'Authorization: Bearer viewer-token' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Viewer calls an admin tool: -32001
curl -s localhost:8000/mcp -H 'Authorization: Bearer viewer-token' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"customer_id":"CUST-10001"}}}'

# Admin calls the same tool: success
curl -s localhost:8000/mcp -H 'Authorization: Bearer admin-token' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"customer_id":"CUST-10001"}}}'

# No token: -32002, HTTP 401
curl -s -w '\n%{http_code}\n' localhost:8000/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/list"}'
```
