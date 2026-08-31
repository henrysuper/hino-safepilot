"""Explicit, raw-preserving parsers for source cells."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Literal

Quality = Literal[
    "valid",
    "missing_structural",
    "missing_unknown",
    "malformed",
    "out_of_range",
]


@dataclass(frozen=True)
class NumericSpec:
    source_name: str
    normalized_name: str
    target: Literal["int", "float"]
    minimum: float | None = None
    maximum: float | None = None
    allowed_values: frozenset[int] | None = None


@dataclass(frozen=True)
class ParsedValue:
    raw: str | None
    value: int | float | datetime | None
    quality: Quality


def raw_string(value: Any) -> str | None:
    """Preserve the source cell representation without inventing a value."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return str(value)


def is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_numeric(
    value: Any,
    spec: NumericSpec,
    *,
    missing_kind: Literal["structural", "unknown"] = "unknown",
) -> ParsedValue:
    raw = raw_string(value)
    if is_missing(value):
        quality: Quality = (
            "missing_structural" if missing_kind == "structural" else "missing_unknown"
        )
        return ParsedValue(raw=raw, value=None, quality=quality)

    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return ParsedValue(raw=raw, value=None, quality="malformed")
    if not number.is_finite():
        return ParsedValue(raw=raw, value=None, quality="malformed")

    if spec.target == "int":
        integral = number.to_integral_value()
        if number != integral:
            return ParsedValue(raw=raw, value=None, quality="malformed")
        parsed: int | float = int(integral)
    else:
        parsed = float(number)
        if not math.isfinite(parsed):
            return ParsedValue(raw=raw, value=None, quality="malformed")

    out_of_range = False
    if spec.minimum is not None and parsed < spec.minimum:
        out_of_range = True
    if spec.maximum is not None and parsed > spec.maximum:
        out_of_range = True
    if spec.allowed_values is not None and parsed not in spec.allowed_values:
        out_of_range = True
    return ParsedValue(
        raw=raw,
        value=parsed,
        quality="out_of_range" if out_of_range else "valid",
    )


def parse_timestamp(
    value: Any,
    *,
    missing_kind: Literal["structural", "unknown"] = "unknown",
) -> ParsedValue:
    raw = raw_string(value)
    if is_missing(value):
        quality: Quality = (
            "missing_structural" if missing_kind == "structural" else "missing_unknown"
        )
        return ParsedValue(raw=raw, value=None, quality=quality)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return ParsedValue(raw=raw, value=None, quality="malformed")
        return ParsedValue(raw=raw, value=value.replace(microsecond=0), quality="valid")
    try:
        parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ParsedValue(raw=raw, value=None, quality="malformed")
    return ParsedValue(raw=raw, value=parsed, quality="valid")


def parse_fence_ids(value: Any) -> tuple[str | None, list[str] | None, Quality]:
    raw = raw_string(value)
    if is_missing(value):
        return raw, None, "missing_structural"
    parts = [part.strip() for part in str(value).split("&")]
    if any(not part for part in parts):
        return raw, None, "malformed"
    return raw, parts, "valid"


def canonical_trip_id(enabled_code: str | None, journey_code: str | None) -> str | None:
    if not enabled_code or not journey_code:
        return None
    digest = sha256(f"{enabled_code}\x1f{journey_code}".encode("utf-8")).hexdigest()
    return f"trip_{digest}"


def year_month(value: datetime | None) -> str:
    return value.strftime("%Y-%m") if value is not None else "UNPARSED"

