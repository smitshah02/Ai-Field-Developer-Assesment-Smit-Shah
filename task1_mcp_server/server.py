import logging
import sys
import uuid

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS
from pydantic import ValidationError

from schemas import Amount, CustomerId, CustomerRecord, Reason, RefundResult

# stdout carries JSON-RPC only; every log line goes to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("customer-server")

CUSTOMERS = {
    "CUST-10001": CustomerRecord(customer_id="CUST-10001", name="Ada Lovelace", email="ada@example.com", tier="gold"),
    "CUST-10002": CustomerRecord(customer_id="CUST-10002", name="Alan Turing", email="alan@example.com", tier="silver"),
    "CUST-12345": CustomerRecord(customer_id="CUST-12345", name="Grace Hopper", email="grace@example.com", tier="bronze"),
}


def invalid_params(name: str, errors: list[dict]) -> MCPError:
    detail = "; ".join(f"{e['field']}: {e['message']}" for e in errors)
    return MCPError(INVALID_PARAMS, f"Invalid arguments for tool {name}: {detail}", {"errors": errors})


class StrictMCPServer(MCPServer):
    """Malformed requests (bad arguments, unknown tool) become JSON-RPC -32602.

    Business failures (e.g. unknown customer) stay as isError results.
    """

    async def call_tool(self, name, arguments, context=None):
        tool = next((t for t in await self.list_tools() if t.name == name), None)
        if tool is not None:
            # The SDK's argument model ignores extra keys, so reject them here.
            unexpected = sorted(set(arguments or {}) - set(tool.input_schema.get("properties", {})))
            if unexpected:
                errors = [{"field": key, "message": "Unexpected argument"} for key in unexpected]
                log.info("Rejected %s unexpected arguments: %s", name, unexpected)
                raise invalid_params(name, errors)
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            if isinstance(exc, UnexpectedToolError):
                raise
            if isinstance(exc.__cause__, ValidationError):
                errors = [
                    {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                    for e in exc.__cause__.errors(include_url=False)
                ]
                log.info("Rejected %s arguments: %s", name, errors)
                raise invalid_params(name, errors) from exc
            if name not in {t.name for t in await self.list_tools()}:
                raise MCPError(INVALID_PARAMS, f"Unknown tool: {name}") from exc
            raise


mcp = StrictMCPServer("customer-server")


def find_customer(customer_id: str) -> CustomerRecord:
    customer = CUSTOMERS.get(customer_id)
    if customer is None:
        raise ToolError(f"Customer {customer_id} not found")
    return customer


@mcp.tool()
def get_customer_record(customer_id: CustomerId) -> CustomerRecord:
    """Look up a customer record by ID (format CUST-XXXXX)."""
    log.info("get_customer_record %s", customer_id)
    return find_customer(customer_id)


@mcp.tool()
def trigger_refund(customer_id: CustomerId, amount: Amount, reason: Reason) -> RefundResult:
    """Issue a refund to a customer. Amount must be > 0 and reason at least 10 characters."""
    find_customer(customer_id)
    refund = RefundResult(refund_id=f"REF-{uuid.uuid4().hex[:12].upper()}", customer_id=customer_id, amount=amount, status="pending")
    log.info("Refund %s for %s: %.2f", refund.refund_id, customer_id, amount)
    return refund


if __name__ == "__main__":
    mcp.run(transport="stdio")
