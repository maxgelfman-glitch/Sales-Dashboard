"""
mock_novig_server.py — A local fake of Novig's WebSocket tape, for tests and simulation.

It lets us rehearse the ugly real-world cases safely:
    * broadcast(msg)       push a price update to every connected client
    * drop_all_clients()   HARSH drop: kill the TCP connection with no close frame
    * silent = True        keep sockets open but stop sending (half-dead stream)
    * required_token       reject handshakes without the right bearer token (HTTP 401)

Nothing here ever touches the real exchange.
"""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus
from typing import Any, Optional

from websockets.asyncio.server import Server, ServerConnection, serve


class MockNovigServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, required_token: Optional[str] = None) -> None:
        self.host = host
        self.port = port
        self.required_token = required_token
        self.clients: set[ServerConnection] = set()
        self.connection_count = 0
        self.auth_headers_seen: list[Optional[str]] = []
        self.silent = False
        self._server: Optional[Server] = None

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/tape"

    async def start(self) -> "MockNovigServer":
        self._server = await serve(self._handler, self.host, self.port, process_request=self._check_auth)
        self.port = self._server.sockets[0].getsockname()[1]  # resolves port=0 to the real one
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.clients.clear()

    def _check_auth(self, connection: ServerConnection, request):
        header = request.headers.get("Authorization")
        self.auth_headers_seen.append(header)
        if self.required_token and header != f"Bearer {self.required_token}":
            return connection.respond(HTTPStatus.UNAUTHORIZED, "invalid token\n")
        return None

    async def _handler(self, ws: ServerConnection) -> None:
        self.clients.add(ws)
        self.connection_count += 1
        try:
            await ws.wait_closed()
        finally:
            self.clients.discard(ws)

    async def broadcast(self, message: Any) -> int:
        """Send a message (dict/list is JSON-encoded) to every client. Returns #recipients."""
        if self.silent:
            return 0
        text = message if isinstance(message, str) else json.dumps(message)
        sent = 0
        for ws in list(self.clients):
            try:
                await ws.send(text)
                sent += 1
            except Exception:  # noqa: BLE001 — a dead client is not the server's problem
                pass
        return sent

    def drop_all_clients(self) -> int:
        """Abort every TCP connection instantly (no WebSocket close handshake)."""
        n = 0
        for ws in list(self.clients):
            ws.transport.abort()
            n += 1
        return n

    async def wait_for_clients(self, count: int = 1, timeout: float = 5.0) -> None:
        async def _poll():
            while len(self.clients) < count:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(_poll(), timeout)


def make_update(
    event_id: str = "NBA-20260923-BOS-NYK",
    league: str = "NBA",
    market_type: str = "moneyline",
    home_team: str = "New York Knicks",
    away_team: str = "Boston Celtics",
    outcome: str = "New York Knicks",
    price: float = 0.50,
    line: Optional[float] = None,
) -> dict[str, Any]:
    """Build one tape message in the (assumed) Novig schema."""
    msg = {
        "league": league, "market_type": market_type, "event_id": event_id,
        "home_team": home_team, "away_team": away_team, "outcome": outcome, "price": price,
    }
    if line is not None:
        msg["line"] = line
    return msg
