"""Request/response models (Pydantic v2)."""

from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


def iso_z(value: datetime) -> str:
    """ISO-8601 UTC with the trailing Z the spec (and most payment APIs) use."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TransactionCreate(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=128)
    source_account: str = Field(min_length=1, max_length=128)
    destination_account: str = Field(min_length=1, max_length=128)
    # Decimal so money is never binary-float rounded; JSON ints/floats still parse.
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("transaction_id", "source_account", "destination_account")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    source_account: str
    destination_account: str
    amount: float
    currency: str
    status: str
    created_at: datetime
    processed_at: datetime | None

    @field_serializer("created_at", "processed_at")
    def _serialize_timestamps(self, value: datetime | None) -> str | None:
        return None if value is None else iso_z(value)


class WebhookAck(BaseModel):
    status: str = "ACCEPTED"
    transaction_id: str
    duplicate: bool


class HealthResponse(BaseModel):
    status: str = "HEALTHY"
    current_time: str
