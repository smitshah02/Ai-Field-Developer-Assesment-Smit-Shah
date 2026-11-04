# AI Field Developer Assessment

Four independent Python tasks covering an MCP server, an MCP security gateway, a streaming PII guardrail and an LLM model router.

## Tasks

| Folder | Summary | README |
|--------|---------|--------|
| [task1_mcp_server](task1_mcp_server/) | Stdio MCP server with strictly validated customer lookup and refund tools | [README](task1_mcp_server/README.md) |
| [task2_mcp_gateway](task2_mcp_gateway/) | FastAPI proxy that authenticates Bearer tokens and blocks `admin_*` tool calls for non-admins | [README](task2_mcp_gateway/README.md) |
| [task3_stream_guardrail](task3_stream_guardrail/) | Streaming LLM gateway that redacts emails, SSNs and card numbers in real time | [README](task3_stream_guardrail/README.md) |
| [task4_model_router](task4_model_router/) | LLM gateway with a per-tenant token rate limiter and primary-to-backup failover | [README](task4_model_router/README.md) |

## Requirements

- Python >= 3.11 for the shared venv, because Task 4 uses `asyncio.timeout`. Tasks 1–3 also work on 3.10.
- Internet access on the first run of Task 4, because tiktoken downloads the `cl100k_base` tokenizer. On offline machines, set `TIKTOKEN_CACHE_DIR` to a pre-populated cache.

## Setup

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Tests

Run each suite from inside its own folder, for example:

```bash
cd task1_mcp_server && pytest -v tests/
```

To run all four:

```bash
for t in task1_mcp_server task2_mcp_gateway task3_stream_guardrail task4_model_router; do
  (cd "$t" && pytest tests/) || echo "FAILED: $t"
done
```

Running `pytest` at the repository root is not supported, because tasks share module and test-file names (`gateway.py`, `tests/test_gateway.py`).

## Ports

| Task | Ports |
|------|-------|
| Task 2 | 9000, 8000 |
| Task 3 | 9100, 8000 |
| Task 4 | 9201, 9202, 8000 |

The gateways all use port 8000, so run the tasks one at a time.
