"""Journal d'audit : une ligne JSON par événement, sans données sensibles.

Pourquoi : quand l'automatisation échoue (ou quand un client conteste), il faut
pouvoir répondre « qu'est-ce qui a été saisi, quand, sur quel site, et pourquoi ».
On journalise donc les étapes, jamais les mots de passe, et on masque e-mail /
téléphone pour limiter l'exposition de données personnelles.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SENSITIVE_KEYWORDS = ("password", "passwd", "secret", "pin", "token", "otp", "code")


def mask_email(value: str) -> str:
    if "@" not in value:
        return "***"
    local, domain = value.split("@", 1)
    return f"{local[:1]}***@{domain}"


def mask_phone(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    return "***" + digits[-3:] if len(digits) >= 3 else "***"


def mask_value(value: str) -> str:
    """Masque une valeur de formulaire potentiellement personnelle."""
    if "@" in value and "." in value.split("@")[-1]:
        return mask_email(value)
    if value.startswith("+") and value[1:].isdigit():
        return mask_phone(value)
    if len(value) > 64:
        return value[:61] + "..."
    return value


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, event: str, **fields: Any) -> None:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
        }
        for key, value in fields.items():
            if any(word in key.lower() for word in SENSITIVE_KEYWORDS):
                payload[key] = "***"
            elif key == "value" and isinstance(value, str):
                payload[key] = mask_value(value)
            else:
                payload[key] = value
        line = json.dumps(payload, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read(self) -> list[Mapping[str, Any]]:
        if not self.path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
