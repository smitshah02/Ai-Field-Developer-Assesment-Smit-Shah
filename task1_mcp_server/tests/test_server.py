import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from server import mcp  # noqa: E402

VALID_REFUND = {"customer_id": "CUST-10001", "amount": 25.5, "reason": "Item arrived damaged"}


async def call(name, args):
    async with Client(mcp) as client:
        return await client.call_tool(name, args)


async def expect_invalid_params(name, args):
    # Catch inside the client context, or the client's task group wraps it in an ExceptionGroup.
    async with Client(mcp) as client:
        with pytest.raises(MCPError) as exc:
            await client.call_tool(name, args)
    assert exc.value.code == -32602
    return exc.value


@pytest.mark.asyncio
async def test_get_customer_record_success():
    result = await call("get_customer_record", {"customer_id": "CUST-12345"})
    assert not result.is_error
    assert result.structured_content["name"] == "Grace Hopper"


@pytest.mark.asyncio
async def test_trigger_refund_success():
    result = await call("trigger_refund", VALID_REFUND)
    assert not result.is_error
    data = result.structured_content
    assert data["refund_id"].startswith("REF-")
    assert data["customer_id"] == "CUST-10001"
    assert data["amount"] == 25.5
    assert data["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, args, bad_field",
    [
        ("get_customer_record", {}, "customer_id"),  # missing field
        ("get_customer_record", {"customer_id": 12345}, "customer_id"),  # wrong type
        ("get_customer_record", {"customer_id": "CUST-123"}, "customer_id"),  # too few digits
        ("get_customer_record", {"customer_id": "cust-12345"}, "customer_id"),  # wrong case
        ("get_customer_record", {"customer_id": "CUST-12345\n"}, "customer_id"),  # trailing newline
        ("get_customer_record", {"customer_id": "CUST-١٢٣٤٥"}, "customer_id"),  # non-ASCII digits
        ("trigger_refund", {**VALID_REFUND, "customer_id": "BAD-1"}, "customer_id"),  # bad ID format
        ("trigger_refund", {"customer_id": "CUST-10001", "reason": "Item arrived damaged"}, "amount"),  # missing
        ("trigger_refund", {**VALID_REFUND, "amount": "25.5"}, "amount"),  # wrong type
        ("trigger_refund", {**VALID_REFUND, "amount": -5}, "amount"),  # negative
        ("trigger_refund", {**VALID_REFUND, "amount": 0}, "amount"),  # zero
        ("trigger_refund", {**VALID_REFUND, "reason": "too short"}, "reason"),  # 9 chars
        ("trigger_refund", {**VALID_REFUND, "reason": None}, "reason"),  # wrong type
    ],
)
async def test_invalid_input_rejected_with_invalid_params(name, args, bad_field):
    err = await expect_invalid_params(name, args)
    assert bad_field in {e["field"] for e in err.data["errors"]}


@pytest.mark.asyncio
async def test_unknown_tool_is_invalid_params():
    await expect_invalid_params("delete_everything", {})


@pytest.mark.asyncio
async def test_unknown_customer_is_tool_error_not_protocol_error():
    result = await call("trigger_refund", {**VALID_REFUND, "customer_id": "CUST-99999"})
    assert result.is_error
    assert "not found" in result.content[0].text


def test_stdout_is_pure_json_rpc():
    """Over real stdio, every stdout line must be JSON-RPC; logs land on stderr."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "get_customer_record", "arguments": {"customer_id": "CUST-10001"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "get_customer_record", "arguments": {"customer_id": "nope"}}},
    ]
    proc = subprocess.Popen(
        [sys.executable, "server.py"], cwd=SERVER_DIR, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Send one message at a time and wait for each reply; closing stdin early can drop in-flight calls.
    replies = {}
    for m in messages:
        proc.stdin.write(json.dumps(m) + "\n")
        proc.stdin.flush()
        if "id" in m:
            reply = json.loads(proc.stdout.readline())  # fails loudly if stdout has any non-JSON line
            assert reply["jsonrpc"] == "2.0"
            replies[reply["id"]] = reply
    proc.stdin.close()
    leftover_stdout = proc.stdout.read()
    stderr = proc.stderr.read()
    proc.wait(timeout=10)

    assert leftover_stdout == ""
    assert replies[2]["result"]["structuredContent"]["customer_id"] == "CUST-10001"
    assert replies[3]["error"]["code"] == -32602
    assert "get_customer_record" in stderr  # our log line went to stderr
