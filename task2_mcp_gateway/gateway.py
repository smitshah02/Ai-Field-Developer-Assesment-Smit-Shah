import json
import os

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from auth import can_call_tool, role_for

DOWNSTREAM_URL = os.environ.get("DOWNSTREAM_URL", "http://127.0.0.1:9000/mcp")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
# Implementation-defined codes for this gateway.
UNAUTHORIZED_TOOL_CALL = -32001
UNAUTHENTICATED = -32002
DOWNSTREAM_UNAVAILABLE = -32003


FORWARD_HEADERS = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version")

app = FastAPI(title="MCP Security Gateway")


def downstream_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


def rpc_error(rid, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}},
        status_code=status,
    )


class DuplicateKeyError(Exception):
    pass


def reject_duplicate_keys(pairs: list[tuple]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise DuplicateKeyError
    return result


@app.post("/mcp")
async def proxy(request: Request) -> Response:
    raw = await request.body()

    try:
        msg = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return rpc_error(None, PARSE_ERROR, "Parse error: body is not valid JSON", 400)
    except DuplicateKeyError:
        # The raw bytes are forwarded, so a downstream parser could pick a different
        # duplicate than the one authorized here. The id can't be trusted either.
        return rpc_error(None, INVALID_REQUEST, "Invalid Request: duplicate JSON keys", 400)
    if not isinstance(msg, dict):
        return rpc_error(None, INVALID_REQUEST, "Invalid Request: expected a single JSON-RPC object", 400)

    is_notification = "id" not in msg
    rid = msg.get("id")
    if not is_notification and (isinstance(rid, bool) or not isinstance(rid, (str, int))):
        return rpc_error(None, INVALID_REQUEST, "Invalid Request: id must be a string or integer", 400)
    method = msg.get("method")
    if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return rpc_error(rid, INVALID_REQUEST, "Invalid Request: need jsonrpc '2.0' and a string method", 400)

    if is_notification and not method.startswith("notifications/"):
        return rpc_error(None, INVALID_REQUEST, f"Invalid Request: '{method}' requires an id", 400)


    role = role_for(request.headers.get("authorization"))
    if role is None:
        return rpc_error(rid, UNAUTHENTICATED, "Unauthenticated: missing or invalid Bearer token", 401)


    if method == "tools/call":
        params = msg.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str) or not name:
            return rpc_error(rid, INVALID_PARAMS, "Invalid params: tools/call requires a string params.name")
        if not can_call_tool(role, name):
            return rpc_error(rid, UNAUTHORIZED_TOOL_CALL,
                             f"Unauthorized Tool Call: role '{role}' may not call '{name}'")

    headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_HEADERS}
    try:
        async with downstream_client() as client:
            upstream = await client.post(DOWNSTREAM_URL, content=raw, headers=headers)
    except httpx.TimeoutException:
        return rpc_error(rid, DOWNSTREAM_UNAVAILABLE, "Downstream MCP server timed out", 504)
    except httpx.HTTPError:
        return rpc_error(rid, DOWNSTREAM_UNAVAILABLE, "Downstream MCP server unavailable", 502)

    passthrough = {k: v for k, v in upstream.headers.items() if k.lower() == "mcp-session-id"}
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=passthrough,
    )
