"""Webhook notifications (Home Assistant, ntfy, Node-RED, n8n, ...)."""

from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.request
from typing import Any

log = logging.getLogger(__name__)


class WebhookNotifier:
    def __init__(self, url: str, timeout_s: float = 5.0):
        self.url = url
        self.timeout_s = timeout_s
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)
        threading.Thread(target=self._run, name="notifier", daemon=True).start()

    def send(self, payload: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            log.warning("Notification queue full - dropping event")

    def _run(self) -> None:
        while True:
            payload = self._queue.get()
            body = json.dumps(payload, ensure_ascii=False).encode()
            req = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Title": payload.get("title", "Nummerplade")})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    resp.read()
            except Exception as exc:  # noqa: BLE001 - never let a webhook kill the pipeline
                log.warning("Webhook failed: %s", exc)
