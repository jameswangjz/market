"""Ordinary refund request/review contracts; no client-supplied financial plans."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RefundRequestBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=10000, strict=True)
    payment_id: str | None = Field(default=None, min_length=1, max_length=36)
    ticket_id: str | None = Field(default=None, min_length=1, max_length=36)
    idempotency_key: str = Field(min_length=8, max_length=100, strict=True)

    @field_validator('reason')
    @classmethod
    def reason_not_blank(cls, value):
        if not value.strip():
            raise ValueError('Reason cannot be blank')
        return value.strip()


class RefundReviewBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['approve', 'reject']
    reason: str = Field(min_length=1, max_length=10000, strict=True)
    expected_version: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=8, max_length=100, strict=True)

    @field_validator('reason')
    @classmethod
    def reason_not_blank(cls, value):
        return RefundRequestBody.reason_not_blank(value)


class RefundExecutionBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: str = Field(min_length=1, max_length=10000, strict=True)
    expected_version: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=8, max_length=100, strict=True)

    @field_validator('reason')
    @classmethod
    def reason_not_blank(cls, value):
        return RefundRequestBody.reason_not_blank(value)


@dataclass(frozen=True)
class PreparedRefundPlan:
    """BE011 server hook result, never parsed from an HTTP request.

    prepare_refund_plan(db, order, refund) must lock/validate original economic
    segments and in-flight reservations and return this or raise unavailable.
    BE010 validates identity/amount/evidence again before freezing the JSON.
    """
    refund_id: str
    order_id: str
    payment_id: str
    amount: Decimal
    calculation_version: str
    as_of: datetime
    evidence_ids: tuple[str, ...]
    economic_plan: dict
    basis: Literal['calls', 'time', 'manual']
