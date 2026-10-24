"""Conditions d'observation d'une page (« la notification X est-elle affichée ? »).

Une condition ne modifie jamais la page : elle se contente de regarder. Trois
types suffisent à couvrir la quasi-totalité des cas réels :

  text          la page contient ce texte visible (ex. « Aucun créneau disponible »)
  selector      un élément correspondant à ce sélecteur est visible (ex. `#reserver`)
  url_contains  l'URL courante contient cette chaîne (ex. `/confirmation`)

L'ordre d'évaluation est décidé par le runner : succès → arrêt → réessai.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .adapters.base import Adapter


class ConditionError(ValueError):
    """Condition invalide (recette mal écrite)."""


SUPPORTED_KINDS = {"text", "selector", "url_contains"}
DEFAULT_TIMEOUT_MS = 3_000


@dataclass(frozen=True)
class Condition:
    kind: str
    value: str
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    description: str = ""

    def label(self) -> str:
        return self.description or f"{self.kind}={self.value!r}"

    def matches(self, adapter: Adapter) -> bool:
        if self.kind == "text":
            return adapter.text_present(self.value, self.timeout_ms)
        if self.kind == "selector":
            return adapter.selector_present(self.value, self.timeout_ms)
        if self.kind == "url_contains":
            return self.value in adapter.current_url()
        raise ConditionError(f"type de condition inconnu : {self.kind!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value}


def condition_from(raw: Mapping[str, Any], where: str) -> Condition:
    if not isinstance(raw, Mapping):
        raise ConditionError(f"{where} : une condition doit être un objet JSON")
    kind = str(raw.get("kind", "")).strip()
    if kind not in SUPPORTED_KINDS:
        raise ConditionError(
            f"{where} : 'kind' doit valoir {', '.join(sorted(SUPPORTED_KINDS))} "
            f"(reçu {kind!r})"
        )
    key = "contains" if kind == "text" else "selector" if kind == "selector" else "url"
    value = raw.get(key, raw.get("value"))
    if value is None:
        value = raw.get("contains") if kind == "text" else raw.get("selector")
    value = str(value or "")
    if not value:
        raise ConditionError(f"{where} : valeur manquante (ex. \"{key}\": \"…\")")
    return Condition(
        kind=kind,
        value=value,
        timeout_ms=int(raw.get("timeout_ms", DEFAULT_TIMEOUT_MS)),
        description=str(raw.get("description", "")),
    )
