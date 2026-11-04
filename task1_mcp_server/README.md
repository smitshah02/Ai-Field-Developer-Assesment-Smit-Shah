# Customer MCP Server

An MCP server, built on the official `mcp` Python SDK (2.x, tested on 2.3.0) and communicating over stdio, that lets an AI client look up customers and issue refunds. Every argument is strictly validated before a tool runs. Customer data is an in-memory sample and refunds are simulated.

## Tools

| Tool | Argument | Rules |
|------|----------|-------|
| `get_customer_record` | `customer_id` | `CUST-` followed by exactly 5 ASCII digits |
| `trigger_refund` | `customer_id` | `CUST-` followed by exactly 5 ASCII digits |
| | `amount` | Finite float > 0. Booleans and numeric strings are rejected |
| | `reason` | At least 10 characters, and not whitespace only |

Both tools reject unknown argument keys.

Sample data: `CUST-10001`, `CUST-10002`, `CUST-12345`. Any other valid-format ID (e.g. `CUST-99999`) returns a "not found" error result.

## Requirements

Python >= 3.10 (the shared repo venv needs 3.11 because of Task 4)

## Project layout

```text
.
├── server.py          # MCP server, tools and error mapping
├── schemas.py         # Pydantic argument and result types
├── README.md
└── tests/
    └── test_server.py
```

## Setup

Follow [Setup](../README.md#setup) in the root README, then `cd task1_mcp_server`.

## Run

```bash
python server.py
```

To use it from an MCP client, add it to the client's config. For example, in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "customer-server": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```

To try it in the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python server.py
```

The server waits for JSON-RPC messages on stdin; it is meant to be launched by an MCP client. Stop it with Ctrl+C.

## Tests

```bash
pytest -v tests/
```

## Error handling

| Failure | Response |
|---------|----------|
| Malformed arguments, including unknown keys | JSON-RPC `-32602`, with per-field details in `error.data.errors` |
| Unknown tool | JSON-RPC `-32602` |
| Unknown customer | Tool result with `isError: true` |
| Unexpected crash | Generic `isError: true` result. Internal details are not leaked |

**Note:** the brief explicitly asks for JSON-RPC error codes. The SDK default, and the current MCP spec guidance, is to return validation failures as `isError` results so the model can self-correct. This server follows the brief.

Example: an unknown argument key.

```json
{"jsonrpc":"2.0","id":2,"error":{"code":-32602,"message":"Invalid arguments for tool trigger_refund: approved_by: Unexpected argument","data":{"errors":[{"field":"approved_by","message":"Unexpected argument"}]}}}
```

## STDIO

stdout carries JSON-RPC only. All logs go to stderr, so they can't corrupt the protocol stream.
