import json

from fastapi import FastAPI, Request, Response

app = FastAPI(title="Mock MCP server")

PROTOCOL_VERSION = "2025-11-25"
RECEIVED: list[dict] = []

TOOLS = [
    {
        "name": "get_customer_record",
        "description": "Fetch a customer record by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    {
        "name": "admin_reset_key",
        "description": "Rotate a customer's API key. Admin only.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    {
        "name": "admin_delete_customer",
        "description": "Delete a customer. Admin only.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
]


def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _call_tool(name: str, args: dict) -> dict | None:
    cid = args.get("customer_id", "unknown")
    if name == "get_customer_record":
        return _text(json.dumps({"customer_id": cid, "name": "Jane Doe", "tier": "gold"}))
    if name == "admin_reset_key":
        return _text(f"API key reset for {cid}")
    if name == "admin_delete_customer":
        return _text(f"Customer {cid} deleted")
    return None


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


@app.post("/mcp")
async def mcp(request: Request):
    try:
        msg = await request.json()
    except ValueError:
        return _error(None, -32700, "Parse error")
    if not isinstance(msg, dict):
        return _error(None, -32600, "Invalid Request")
    RECEIVED.append(msg)

    if "id" not in msg:
        return Response(status_code=202)

    rid = msg["id"]
    method = msg.get("method")
    params = msg.get("params", {})
    if not isinstance(params, dict):
        return _error(rid, -32602, "Invalid params: params must be an object")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mock-mcp-server", "version": "1.0.0"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(args, dict):
            return _error(rid, -32602, "Invalid params: name must be a string and arguments an object")
        result = _call_tool(name, args)
        if result is not None:
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        return _error(rid, -32602, f"Unknown tool: {name}")
    return _error(rid, -32601, f"Method not found: {method}")
