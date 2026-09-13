"""SDK-reported main-loop usage, never pricing or inferred model identity."""

from typing import Any, Literal

from pydantic import BaseModel


class Usage(BaseModel):
    input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    total_input_tokens: int | None = None
    output_tokens: int | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None


FIELDS = tuple(Usage.model_fields)
TOKEN_FIELDS = FIELDS[:3] + ("output_tokens",)


def _count(value: Any) -> int | None:
    # Do not coerce booleans, strings, negative/NaN values, or absent fields.
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def extract_usage(message: Any) -> Usage:
    raw = getattr(message, "usage", None)
    raw = raw if isinstance(raw, dict) else {}
    values = {key: _count(raw.get(key)) for key in TOKEN_FIELDS}
    for key in ("num_turns", "duration_ms", "duration_api_ms"):
        values[key] = _count(getattr(message, key, None))
    inputs = [values[key] for key in TOKEN_FIELDS[:3]]
    values["total_input_tokens"] = sum(inputs) if all(v is not None for v in inputs) else None
    return Usage(**values)


class UsageTotal(Usage):
    calls: int = 0
    reported_calls: int = 0
    # These are sums of reported fields, not billing or whole-harness totals.
    complete: bool = False


class JobUsage(BaseModel):
    scope: Literal["sdk_reported_main_loop"] = "sdk_reported_main_loop"
    execution: UsageTotal
    review: UsageTotal
    total: UsageTotal


def aggregate_usage(rows: list[dict]) -> JobUsage:
    def total(items: list[dict]) -> UsageTotal:
        reports = [item.get("usage") for item in items]
        values = {}
        # A partially known total is unknown, not a misleading partial sum.
        for key in FIELDS:
            parts = [r.get(key) if r else None for r in reports]
            values[key] = sum(parts) if parts and all(v is not None for v in parts) else None
        return UsageTotal(
            **values, calls=len(items),
            reported_calls=sum(r is not None for r in reports),
            complete=bool(items) and all(v is not None for v in values.values()),
        )
    return JobUsage(
        execution=total([r for r in rows if r["stage"] == "execution"]),
        review=total([r for r in rows if r["stage"] == "review"]),
        total=total(rows),
    )
