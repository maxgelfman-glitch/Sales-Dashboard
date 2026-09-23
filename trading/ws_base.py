"""
ws_base.py — Self-healing WebSocket client shared by every exchange feed.

Novig (novig_feed.py) and Kalshi (kalshi_feed.py) both subclass
ResilientWebSocketFeed, so the reconnect / heartbeat / watchdog behaviour
proven in tests/test_feed.py is identical on every venue.

A subclass supplies four hooks:
    _headers()          auth headers for THIS handshake (called on every reconnect)
    _on_open(ws)        send subscription messages right after the handshake
    _handle_raw(raw)    parse one message and update local state
    _clear_state()      wipe every local book/price; returns counts for the log

GUARANTEES
    * Nothing raised inside the loop can crash the parent process.
    * On any drop: log, wipe local state (never trade on stale prices),
      wait RECONNECT_DELAY_SECONDS (2.5s), re-handshake.
    * Protocol ping/pong every PING_INTERVAL_SECONDS; a silent socket is
      killed after STALE_STREAM_SECONDS by the watchdog.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional, Union
from urllib.parse import urlparse

from websockets.asyncio.client import connect

RECONNECT_DELAY_SECONDS = 2.5   # cool-off before a re-handshake (keeps recovery < 3s)
PING_INTERVAL_SECONDS = 10.0
PING_TIMEOUT_SECONDS = 10.0
STALE_STREAM_SECONDS = 30.0
OPEN_TIMEOUT_SECONDS = 10.0

StateCallback = Callable[[str, dict], Union[None, Awaitable[None]]]


class StaleStreamError(Exception):
    """Raised when the socket is open but silent for too long."""


async def safe_call(fn, *args, logger: logging.Logger) -> None:
    """Run a user callback; a bug in it must never take a feed down."""
    if fn is None:
        return
    try:
        result = fn(*args)
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # noqa: BLE001
        logger.exception("CALLBACK_ERROR in %s (feed continues)", getattr(fn, "__name__", fn))


class ResilientWebSocketFeed:
    venue = "generic"

    def __init__(
        self,
        url: str,
        on_state_change: Optional[StateCallback] = None,
        reconnect_delay: float = RECONNECT_DELAY_SECONDS,
        ping_interval: float = PING_INTERVAL_SECONDS,
        ping_timeout: float = PING_TIMEOUT_SECONDS,
        stale_after: float = STALE_STREAM_SECONDS,
        open_timeout: float = OPEN_TIMEOUT_SECONDS,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.url = url
        self.on_state_change = on_state_change
        self.reconnect_delay = reconnect_delay
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.stale_after = stale_after
        self.open_timeout = open_timeout
        self.log = logger or logging.getLogger(f"trading.{self.venue}")

        self.connected = asyncio.Event()
        self.connect_count = 0
        self.disconnect_count = 0
        self.messages_received = 0
        self.last_recovery_seconds: Optional[float] = None

        self._stopping = False
        self._ws = None
        self._dropped_at: Optional[float] = None
        self._background: set[asyncio.Task] = set()   # keeps fire-and-forget tasks alive until done

    # ---------------- hooks for subclasses ----------------
    def _headers(self) -> Optional[dict[str, str]]:
        return None

    async def _on_open(self, ws) -> None:
        return None

    async def _handle_raw(self, raw: Union[str, bytes]) -> None:
        raise NotImplementedError

    def _clear_state(self) -> dict[str, int]:
        return {}

    def _heartbeat_extra(self) -> str:
        return ""

    # ---------------- public API ----------------
    async def run(self) -> None:
        """Connect, stream, and reconnect forever until stop() is called."""
        while not self._stopping:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — by design: nothing may kill the loop
                if self._stopping:
                    break
                self._handle_drop(exc)
            else:
                if self._stopping:
                    break
                self._handle_drop(ConnectionError("server closed the stream"))
            if self._stopping:
                break
            self.log.info("CONN %s reconnecting in %.1fs", self.venue, self.reconnect_delay)
            await asyncio.sleep(self.reconnect_delay)
        self.connected.clear()
        await self._emit_state("STOPPED", {"venue": self.venue})
        self.log.info("CONN %s feed stopped", self.venue)

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None:
            await self._ws.close()

    # ---------------- internals ----------------
    def _connect_kwargs(self) -> dict[str, Any]:
        host = urlparse(self.url).hostname or ""
        return dict(
            additional_headers=self._headers(),
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            open_timeout=self.open_timeout,
            proxy=None if host in {"localhost", "127.0.0.1", "::1"} else True,
        )

    async def _session(self) -> None:
        await self._emit_state("CONNECTING", {"venue": self.venue, "url": self.url})
        async with connect(self.url, **self._connect_kwargs()) as ws:
            self._ws = ws
            await self._on_open(ws)
            self.connect_count += 1
            self.connected.set()
            details: dict[str, Any] = {"venue": self.venue, "url": self.url, "connect_count": self.connect_count}
            if self._dropped_at is not None:
                self.last_recovery_seconds = time.monotonic() - self._dropped_at
                details["recovery_seconds"] = round(self.last_recovery_seconds, 3)
                self._dropped_at = None
            self.log.info("CONN connected %s", json.dumps(details))
            await self._emit_state("CONNECTED", details)

            heartbeat = asyncio.create_task(self._heartbeat_logger(ws))
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_after)
                    except asyncio.TimeoutError:
                        raise StaleStreamError(f"no data for {self.stale_after:.1f}s")
                    self.messages_received += 1
                    await self._handle_raw(raw)
            finally:
                heartbeat.cancel()
                self._ws = None

    async def _heartbeat_logger(self, ws) -> None:
        while True:
            await asyncio.sleep(self.ping_interval)
            self.log.info("HEARTBEAT %s ping latency=%.1fms msgs=%d%s", self.venue, ws.latency * 1000,
                          self.messages_received, self._heartbeat_extra())

    def _handle_drop(self, exc: BaseException) -> None:
        """Log the failure and wipe stale state. Must never raise."""
        was_connected = self.connected.is_set()
        self.connected.clear()
        cleared = self._clear_state()
        if was_connected:
            self.disconnect_count += 1
            self._dropped_at = time.monotonic()
        elif self._dropped_at is None:
            self._dropped_at = time.monotonic()
        summary = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in cleared.items()) or "nothing"
        self.log.warning("CONN %s dropped (%s: %s); cleared %s", self.venue, type(exc).__name__, exc, summary)
        details = {"venue": self.venue, "error": f"{type(exc).__name__}: {exc}", **{f"cleared_{k}": v
                                                                                     for k, v in cleared.items()}}
        task = asyncio.get_running_loop().create_task(self._emit_state("DISCONNECTED", details))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _emit_state(self, state: str, details: dict) -> None:
        await safe_call(self.on_state_change, state, details, logger=self.log)
