"""
novig_private.py — Parsing of Novig private execution slips (our own fills).

Novig carries public prices AND private execution data on ONE WebSocket (per the
brief and Novig's docs). NovigFeed (novig_feed.py) subscribes to both channels on
that socket and hands every slip it sees to the supervisor via parse_slips().

SLIP FIELDS
    order_id       our order's id (as returned by POST /v1/orders)
    status         FILLED | PARTIAL   (CANCELLED/CANCELED/EXPIRED/REJECTED also accepted)
    filled_volume  running CUMULATIVE total filled for the life of that order id
                   (confirmed in the brief). The supervisor books
                   delta = filled_volume - previous filled_volume, so duplicate or
                   replayed slips add nothing. "incremental" remains available as a
                   setting only in case Novig's behaviour ever changes.
    price_cents    execution price
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Union

from pydantic import AliasChoices, BaseModel, Field, ValidationError, field_validator

TERMINAL_STATUSES = {"FILLED", "CANCELLED", "CANCELED", "EXPIRED", "REJECTED"}

log = logging.getLogger("trading.private")


class FillSlip(BaseModel):
    order_id: str = Field(validation_alias=AliasChoices("order_id", "orderId"))
    status: str
    filled_volume: float = Field(ge=0, validation_alias=AliasChoices("filled_volume", "filledVolume"))
    price_cents: Optional[float] = Field(default=None, validation_alias=AliasChoices("price_cents", "priceCents"))
    venue: str = "novig"            # which exchange's execution channel produced it ("kalshi" fills are incremental)

    @field_validator("order_id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> Any:
        return str(v) if isinstance(v, int) else v

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v: Any) -> Any:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("price_cents")
    @classmethod
    def _price(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not 0 < v < 100:
            raise ValueError(f"price_cents must be strictly between 0 and 100, got {v}")
        return v

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def parse_slips(raw: Union[str, bytes]) -> list[FillSlip]:
    """Slip, list of slips, or {"event"|"type": ..., "data": slip | [slips]}. Never raises."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("PRIVATE_PARSE_ERROR non-JSON message skipped")
        return []
    if isinstance(payload, dict) and isinstance(payload.get("data"), (list, dict)):
        payload = payload["data"]
    out = []
    for item in payload if isinstance(payload, list) else [payload]:
        if not isinstance(item, dict) or not any(k in item for k in ("order_id", "orderId")):
            continue
        try:
            out.append(FillSlip.model_validate(item))
        except ValidationError as exc:
            log.warning("PRIVATE_PARSE_ERROR bad slip (%d errors): %s", exc.error_count(), item)
    return out
