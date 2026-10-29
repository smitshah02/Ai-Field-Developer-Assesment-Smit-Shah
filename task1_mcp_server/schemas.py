from typing import Annotated, Literal

from pydantic import BaseModel, Field

# [0-9] not \d: \d also matches non-ASCII digits like "٣".
CustomerId = Annotated[str, Field(pattern=r"^CUST-[0-9]{5}$", strict=True, description="Format CUST-XXXXX, e.g. CUST-12345")]
Amount = Annotated[float, Field(gt=0, strict=True, allow_inf_nan=False, description="Refund amount, must be > 0")]
Reason = Annotated[str, Field(min_length=10, strict=True, description="Why the refund is issued, at least 10 characters")]


class CustomerRecord(BaseModel):
    customer_id: str
    name: str
    email: str
    tier: str


class RefundResult(BaseModel):
    refund_id: str
    customer_id: str
    amount: float
    status: Literal["pending"]
