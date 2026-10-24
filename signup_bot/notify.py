"""Notifications de fin d'exécution.

Deux canaux, volontairement simples :
  - un « outbox » JSONL (toujours écrit) : lisible par un humain, un cron ou le
    futur dashboard ;
  - un webhook optionnel (URL fournie par variable d'environnement), pour brancher
    Slack/Telegram/n8n sans dépendance supplémentaire.

Le message ne contient jamais de mot de passe ni de code OTP. Il peut contenir
l'identifiant de compte, le site, le résultat et la raison.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_EVENTS = ("success", "stopped", "abandoned", "failed")


class Notifier:
    def __init__(
        self,
        outbox: Path,
        *,
        events: Sequence[str] = DEFAULT_EVENTS,
        webhook_env: str = "",
        timeout_seconds: float = 5.0,
    ):
        self.outbox = Path(outbox)
        self.events = tuple(events)
        self.webhook_env = webhook_env
        self.timeout_seconds = timeout_seconds
        self.sent: list[dict[str, Any]] = []

    def enabled(self, event: str) -> bool:
        return event in self.events

    def send(self, event: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        if not self.enabled(event):
            return None
        message = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **{k: v for k, v in payload.items()},
        }
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        with self.outbox.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.sent.append(message)
        self._post_webhook(message)
        return message

    def _post_webhook(self, message: Mapping[str, Any]) -> None:
        url = os.environ.get(self.webhook_env, "") if self.webhook_env else ""
        if not url.startswith(("http://", "https://")):
            return
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:  # une notification ne doit jamais faire échouer l'exécution
            urllib.request.urlopen(request, timeout=self.timeout_seconds).close()
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
