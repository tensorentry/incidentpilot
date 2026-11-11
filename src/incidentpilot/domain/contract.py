"""Base for stored and model-facing contracts."""

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    """Unknown fields are rejected, values are immutable, and nothing is coerced.

    Strict mode still accepts JSON strings for UUIDs and times, so model output
    validates from JSON while Python callers must pass real types.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
