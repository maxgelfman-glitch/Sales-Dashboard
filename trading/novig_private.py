"""
novig_private.py — Novig private order channel (our own fills).

Novig's docs (search result; docs site unreachable from the build sandbox) say
private place / fill / cancel notifications arrive over the NBX WebSocket API.
This module listens for "execution slips" and hands each one to the supervisor,
which updates positions and the global exposure count in real time.

SLIP FIELDS (per the brief)
    order_id       our order's id (as returned by POST /v1/orders)
    status         FILLED | PARTIAL   (CANCELLED/CANCELED/EXPIRED also accepted)
    filled_volume  contracts filled — see FILL_VOLUME_MODE below
    price_cents    execution price

FILL_VOLUME_MODE (must be set explicitly for live trading: NOVIG_FILL_VOLUME_MODE)
    "cumulative"   filled_volume = TOTAL filled so far for the order. Duplicate or
                   replayed slips are harmless (delta <= 0 is ignored).
    "incremental"  filled_volume = contracts filled by THIS slip.
    Getting this wrong mis-states positions (cumulative read as incremental
    over-counts; incremental read as cumulative UNDER-counts exposure), which is
    why the engine refuses to go live until it is set.

ASSUMED (confirm with Novig): the channel URL (NOVIG_PRIVATE_WS_URL — the brief's
"wss://://novig.com" is not a valid URL) and the subscribe message
(NOVIG_PRIVATE_SUBSCRIBE, default {"event": "subscribe", "data": "orders"}).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable, Iterable, Optional, Union

from pydantic import AliasChoices, BaseModel, Field, ValidationError, field_validator

from ws_base import RECONNECT_DELAY_SECONDS, ResilientWebSocketFeed, StateCallback, safe_call

DEFAULT_PRIVATE_SUBSCRIBE = {"event": "subscribe", "data": "orders"}
TERMINAL_STATUSES = {"FILLED", "CANCELLED", "CANCELED", "EXPIRED", "REJECTED"}

log = logging.getLogger("trading.private")


class FillSlip(BaseModel):
    order_id: str = Field(validation_alias=AliasChoices("order_id", "orderId"))
    status: str
    filled_volume: float = Field(ge=0, validation_alias=AliasChoices("filled_volume", "filledVolume"))
    price_cents: Optional[float] = Field(default=None, validation_alias=AliasChoices("price_cents", "priceCents"))

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


SlipCallback = Callable[[FillSlip], Union[None, Awaitable[None]]]


class NovigPrivateFeed(ResilientWebSocketFeed):
    venue = "novig_private"

    def __init__(self, url: str, token: Optional[str] = None, on_slip: Optional[SlipCallback] = None,
                 on_state_change: Optional[StateCallback] = None,
                 subscribe_messages: Optional[Iterable[dict]] = None,
                 reconnect_delay: float = RECONNECT_DELAY_SECONDS, **kw: Any) -> None:
        # Fills can be hours apart: liveness comes from ping/pong, not message flow,
        # so the silent-stream watchdog is relaxed to one hour for this channel.
        kw.setdefault("stale_after", 3600.0)
        super().__init__(url, on_state_change, reconnect_delay, logger=log, **kw)
        self._token = token if token is not None else os.environ.get("NOVIG_BEARER_TOKEN")
        self.on_slip = on_slip
        self.subscribe_messages = [DEFAULT_PRIVATE_SUBSCRIBE] if subscribe_messages is None else list(subscribe_messages)
        self.slips_received = 0

    def _headers(self) -> Optional[dict[str, str]]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else None

    async def _on_open(self, ws) -> None:
        for msg in self.subscribe_messages:
            await ws.send(json.dumps(msg))
            log.info("CONN novig_private sent subscription %s", json.dumps(msg))

    async def _handle_raw(self, raw) -> None:
        for slip in parse_slips(raw):
            self.slips_received += 1
            log.info("FILL_SLIP %s", slip.model_dump_json())
            await safe_call(self.on_slip, slip, logger=log)
