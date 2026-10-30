import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway  
import mock_server 

VIEWER = {"Authorization": "Bearer viewer-token"}
ADMIN = {"Authorization": "Bearer admin-token"}


@pytest.fixture
def client(monkeypatch):
    """Gateway under test, with its downstream httpx client wired in-process to the mock."""
    mock_server.RECEIVED.clear()
    monkeypatch.setattr(
        gateway, "downstream_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_server.app)),
    )
    return TestClient(gateway.app)


def call(name, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": name, "arguments": {"customer_id": "CUST-10001"}}}


def test_viewer_tools_list_forwarded(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, headers=VIEWER)
    assert r.status_code == 200
    assert r.json()["id"] == 7
    assert {t["name"] for t in r.json()["result"]["tools"]} == {
        "get_customer_record", "admin_reset_key", "admin_delete_customer"}
    assert len(mock_server.RECEIVED) == 1


def test_viewer_allowed_get_customer_record(client):
    r = client.post("/mcp", json=call("get_customer_record"), headers=VIEWER)
    assert r.status_code == 200
    assert "Jane Doe" in r.json()["result"]["content"][0]["text"]
    assert len(mock_server.RECEIVED) == 1


@pytest.mark.parametrize("tool", ["admin_reset_key", "admin_delete_customer", "Admin_Reset_Key"])
def test_viewer_blocked_on_admin_tools_and_never_forwarded(client, tool):
    r = client.post("/mcp", json=call(tool, rid="req-42"), headers=VIEWER)
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == "req-42"
    assert body["error"]["code"] == -32001
    assert "Unauthorized Tool Call" in body["error"]["message"]
    assert "result" not in body
    assert mock_server.RECEIVED == []  


@pytest.mark.parametrize("tool,text", [("admin_reset_key", "API key reset"),
                                       ("admin_delete_customer", "deleted"),
                                       ("get_customer_record", "Jane Doe")])
def test_admin_allowed_all_tools(client, tool, text):
    r = client.post("/mcp", json=call(tool), headers=ADMIN)
    assert text in r.json()["result"]["content"][0]["text"]
    assert mock_server.RECEIVED == [call(tool)]  # forwarded unmodified


def test_downstream_response_returned_unchanged(client):
    msg = call("get_customer_record", rid=3)
    via_gateway = client.post("/mcp", json=msg, headers=ADMIN)
    direct = TestClient(mock_server.app).post("/mcp", json=msg)
    assert via_gateway.status_code == direct.status_code
    assert via_gateway.content == direct.content


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong-token"},
    {"Authorization": "Basic admin-token"},
    {"Authorization": "Bearer "},
    {"Authorization": "admin-token"},
])
def test_bad_or_missing_token_rejected_with_original_id(client, headers):
    r = client.post("/mcp", json=call("get_customer_record", rid=99), headers=headers)
    assert r.status_code == 401
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 99
    assert body["error"]["code"] == -32002
    assert mock_server.RECEIVED == []


def test_malformed_json_is_parse_error(client):
    r = client.post("/mcp", content=b"{not json", headers={**ADMIN, "content-type": "application/json"})
    assert r.json() == {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error: body is not valid JSON"}}
    assert mock_server.RECEIVED == []


@pytest.mark.parametrize("payload,rid", [
    ([call("get_customer_record")], None),              
    ({"id": 5, "method": "tools/list"}, 5),             
    ({"jsonrpc": "2.0", "id": 6, "method": 123}, 6),
])
def test_invalid_request(client, payload, rid):
    r = client.post("/mcp", json=payload, headers=ADMIN)
    assert r.json()["error"]["code"] == -32600
    assert r.json()["id"] == rid
    assert mock_server.RECEIVED == []


@pytest.mark.parametrize("params", [None, {}, {"name": 42}, "admin_reset_key"])
def test_tools_call_without_valid_name_is_invalid_params(client, params):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": params},
                    headers=VIEWER)
    assert r.json()["error"]["code"] == -32602
    assert mock_server.RECEIVED == []


def gateway_with_downstream(monkeypatch, handler):
    """Gateway whose downstream is httpx's built-in MockTransport running `handler`."""
    monkeypatch.setattr(
        gateway, "downstream_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return TestClient(gateway.app)


def raises(exc):
    def handler(request):
        raise exc("secret-internal-host:9000 exploded", request=request)
    return handler


@pytest.mark.parametrize("exc,status,message", [
    (httpx.ConnectError, 502, "Downstream MCP server unavailable"),
    (httpx.ReadTimeout, 504, "Downstream MCP server timed out"),
])
def test_downstream_failure_returns_generic_jsonrpc_error(monkeypatch, exc, status, message):
    client = gateway_with_downstream(monkeypatch, raises(exc))
    r = client.post("/mcp", json=call("get_customer_record", rid=11), headers=VIEWER)
    assert r.status_code == status
    assert r.json() == {"jsonrpc": "2.0", "id": 11, "error": {"code": -32003, "message": message}}
    assert "secret-internal-host" not in r.text


def test_mcp_headers_forwarded_and_auth_and_unknown_headers_dropped(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})

    client = gateway_with_downstream(monkeypatch, handler)
    client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={
        **ADMIN,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
        "Mcp-Session-Id": "sess-1",
        "Mcp-Method": "tools/call",   
        "Mcp-Name": "admin_reset_key",
        "X-Forwarded-Host": "evil.example",
    })
    assert seen["mcp-protocol-version"] == "2025-11-25"
    assert seen["mcp-session-id"] == "sess-1"
    assert seen["accept"] == "application/json, text/event-stream"
    assert seen["content-type"] == "application/json"
    for dropped in ("authorization", "mcp-method", "mcp-name", "x-forwarded-host"):
        assert dropped not in seen


def test_downstream_session_id_and_status_preserved(monkeypatch):
    def handler(request):
        return httpx.Response(404, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "x"}},
                              headers={"Mcp-Session-Id": "sess-9", "Set-Cookie": "internal=1"})

    r = gateway_with_downstream(monkeypatch, handler).post("/mcp", json=call("get_customer_record"), headers=VIEWER)
    assert r.status_code == 404
    assert r.headers["mcp-session-id"] == "sess-9"
    assert "set-cookie" not in r.headers


def test_unknown_non_admin_tool_is_forwarded(client):
    r = client.post("/mcp", json=call("get_order_history"), headers=VIEWER)
    assert r.json()["error"] == {"code": -32602, "message": "Unknown tool: get_order_history"}
    assert len(mock_server.RECEIVED) == 1 


@pytest.mark.parametrize("arguments", ["CUST-10001", ["CUST-10001"], None])
def test_malformed_arguments_get_invalid_params_from_downstream(client, arguments):
    msg = {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
           "params": {"name": "get_customer_record", "arguments": arguments}}
    r = client.post("/mcp", json=msg, headers=VIEWER)
    assert r.status_code == 200
    assert r.json()["id"] == 12 and r.json()["error"]["code"] == -32602


def test_notification_forwarded_as_202_with_empty_body(client):
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    r = client.post("/mcp", json=note, headers=VIEWER)
    assert r.status_code == 202
    assert r.content == b""
    assert mock_server.RECEIVED == [note]


def test_unauthenticated_notification_rejected(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 401
    assert r.json()["id"] is None and r.json()["error"]["code"] == -32002
    assert mock_server.RECEIVED == []


@pytest.mark.parametrize("headers", [VIEWER, ADMIN])
def test_id_less_tool_call_rejected_and_never_forwarded(client, headers):
    msg = call("admin_reset_key")
    del msg["id"]
    r = client.post("/mcp", json=msg, headers=headers)
    assert r.status_code == 400
    assert r.json()["id"] is None and r.json()["error"]["code"] == -32600
    assert mock_server.RECEIVED == []


@pytest.mark.parametrize("rid", [None, 1.5, True])
def test_null_float_or_bool_id_is_invalid_request(client, rid):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": rid, "method": "tools/list"}, headers=ADMIN)
    assert r.status_code == 400 and r.json()["error"]["code"] == -32600
    assert mock_server.RECEIVED == []


def test_initialize_handshake_through_gateway(client):
    r = client.post("/mcp", headers=VIEWER, json={
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                   "clientInfo": {"name": "test-client", "version": "1.0"}}})
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "mock-mcp-server"

    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=VIEWER)
    assert r.status_code == 202

    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=VIEWER)
    assert len(r.json()["result"]["tools"]) == 3
