"""
alerts.py — Push CRITICAL engine events to your phone / chat.

Set ALERT_WEBHOOK_URL to any incoming-webhook URL; the engine POSTs JSON
{"text": ..., "content": ...} (Slack reads "text", Discord reads "content"), or a plain-text body
for ntfy.sh topics (https://ntfy.sh/<your-topic>, free, has a phone app).

What triggers an alert: every CRITICAL log line, e.g.
    exposure kill-switch engaged        daily loss stop hit
    Novig socket down in live mode      order possibly filled unseen (LIVE_UNCONFIRMED)
    unknown fill / cancel failed        position mismatch with the exchange
    PRICE_IMPROVEMENT_MISSING           settlement with unknown P&L

Design: a logging.Handler that hands messages to one background thread, so the trading loop never
waits on the network. Identical messages are sent at most once per 5 minutes. Never raises.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.request
from typing import Optional

DEDUPE_SECONDS = 300.0
TIMEOUT_SECONDS = 5.0


class AlertHandler(logging.Handler):
    def __init__(self, url: str, level: int = logging.CRITICAL, prefix: str = "[trading engine]",
                 sender=None) -> None:
        super().__init__(level)
        self.url = url
        self.prefix = prefix
        self._sent: dict[str, float] = {}
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=200)
        self._send = sender or self._post
        self.sent = 0
        self._thread = threading.Thread(target=self._worker, name="alerts", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            key = msg[:120]
            now = time.time()
            if now - self._sent.get(key, 0.0) < DEDUPE_SECONDS:
                return
            self._sent[key] = now
            self._queue.put_nowait(f"{self.prefix} {record.levelname} {record.name}: {msg}"[:1800])
        except Exception:  # noqa: BLE001 — an alert must never break trading
            pass

    def _post(self, text: str) -> None:
        if "ntfy.sh/" in self.url:
            data, headers = text.encode(), {"Content-Type": "text/plain"}
        else:
            data, headers = json.dumps({"text": text, "content": text}).encode(), {"Content-Type": "application/json"}
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS).close()

    def _worker(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                return
            try:
                self._send(text)
                self.sent += 1
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("trading.alerts").warning("ALERT could not be sent: %s", exc)

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        super().close()

    def flush_for_tests(self, timeout: float = 2.0) -> None:
        end = time.time() + timeout
        while not self._queue.empty() and time.time() < end:
            time.sleep(0.01)
        time.sleep(0.05)
