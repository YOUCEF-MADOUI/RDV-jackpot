"""« Recettes » : description déclarative du parcours d'inscription d'un site.

Une recette = un fichier JSON qui liste les étapes (goto, fill, upload, click…)
sans écrire de code spécifique au site. Ajouter un site = ajouter une recette,
pas un nouveau programme. C'est ce qui rend le système maintenable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

SUPPORTED_ACTIONS = {
    "goto",
    "fill",
    "select",
    "check",
    "upload",
    "click",
    "press",
    "wait_for",
    "assert_text",
    "pause_for_human",
    "wait_ms",
}
ACTIONS_REQUIRING_SELECTOR = {
    "fill",
    "select",
    "check",
    "upload",
    "click",
    "press",
    "wait_for",
}
ACTIONS_REQUIRING_VALUE = {"fill", "select", "upload", "press", "goto", "wait_ms"}

TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class RecipeError(ValueError):
    """Recette invalide."""


def render(template: str, mapping: Mapping[str, str]) -> str:
    """Remplace {{ first_name }} par la valeur du profil. Échoue si inconnu."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in mapping:
            raise RecipeError(
                f"variable inconnue {{{{ {key} }}}} — valeurs disponibles : "
                + ", ".join(sorted(mapping))
            )
        return str(mapping[key])

    return TEMPLATE_RE.sub(replace, template)


@dataclass(frozen=True)
class Step:
    action: str
    selector: str = ""
    value: str = ""
    contains: str = ""
    prompt: str = ""
    secret_env: str = ""
    timeout_ms: int = 15_000

    def describe(self) -> str:
        target = self.selector or self.value or self.contains or ""
        return f"{self.action}({target})"


@dataclass(frozen=True)
class Recipe:
    site: str
    start_url: str
    allowed_hosts: tuple[str, ...]
    authorized: bool
    authorization_note: str = ""
    steps: tuple[Step, ...] = field(default_factory=tuple)
    success_text: str = ""
    min_interval_seconds: int = 60
    user_agent: str = ""
    locale: str = "fr-FR"
    source_path: Path | None = None

    def assert_url_allowed(self, url: str) -> None:
        """Garde-fou : on ne visite QUE les domaines déclarés dans la recette."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise RecipeError(f"schéma d'URL refusé : {parsed.scheme!r}")
        host = (parsed.hostname or "").lower()
        for allowed in self.allowed_hosts:
            allowed = allowed.lower()
            if host == allowed or (allowed.startswith("*.") and host.endswith(allowed[1:])):
                return
        raise RecipeError(
            f"hôte non autorisé : {host!r}. Domaines permis par la recette : "
            + ", ".join(self.allowed_hosts)
        )

    def requires_human_pause(self) -> bool:
        """Vrai si le parcours contient un point de passage humain (CAPTCHA, OTP…)."""
        return any(step.action == "pause_for_human" for step in self.steps)

    def check_authorization(self) -> None:
        if not self.authorized:
            raise RecipeError(
                "recette non autorisée : renseignez \"authorized\": true uniquement si vous "
                "êtes propriétaire du site ou disposez d'une autorisation écrite. "
                "Ce projet ne doit pas servir à créer des comptes en masse sur un service tiers."
            )


def _step_from(raw: Mapping[str, Any], index: int) -> Step:
    try:
        action = str(raw["action"]).strip()
    except KeyError as exc:
        raise RecipeError(f"étape {index + 1} : champ 'action' manquant") from exc
    if action not in SUPPORTED_ACTIONS:
        raise RecipeError(
            f"étape {index + 1} : action inconnue {action!r} "
            f"(supportées : {', '.join(sorted(SUPPORTED_ACTIONS))})"
        )
    selector = str(raw.get("selector", ""))
    value = str(raw.get("value", ""))
    secret_env = str(raw.get("secret_env", ""))
    if action in ACTIONS_REQUIRING_SELECTOR and not selector:
        raise RecipeError(f"étape {index + 1} ({action}) : 'selector' obligatoire")
    if action in ACTIONS_REQUIRING_VALUE and value == "" and not secret_env:
        raise RecipeError(
            f"étape {index + 1} ({action}) : 'value' (ou 'secret_env') obligatoire"
        )
    if secret_env and action not in ("fill", "select"):
        raise RecipeError(
            f"étape {index + 1} : 'secret_env' n'est accepté que pour fill/select"
        )
    if action == "assert_text" and not raw.get("contains"):
        raise RecipeError(f"étape {index + 1} (assert_text) : 'contains' obligatoire")
    return Step(
        action=action,
        selector=selector,
        value=value,
        contains=str(raw.get("contains", "")),
        prompt=str(raw.get("prompt", "")),
        secret_env=secret_env,
        timeout_ms=int(raw.get("timeout_ms", 15_000)),
    )


def load_recipe(path: str | Path) -> Recipe:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise RecipeError(f"recette introuvable : {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecipeError(f"JSON invalide dans {path.name} : {exc}") from exc

    for key in ("site", "start_url", "allowed_hosts"):
        if key not in raw:
            raise RecipeError(f"champ obligatoire manquant dans la recette : {key!r}")

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise RecipeError("la recette doit contenir au moins une étape dans 'steps'")

    recipe = Recipe(
        site=str(raw["site"]),
        start_url=str(raw["start_url"]),
        allowed_hosts=tuple(str(h) for h in raw["allowed_hosts"]),
        authorized=bool(raw.get("authorized", False)),
        authorization_note=str(raw.get("authorization_note", "")),
        steps=tuple(_step_from(step, i) for i, step in enumerate(steps_raw)),
        success_text=str(raw.get("success_text", "")),
        min_interval_seconds=int(raw.get("min_interval_seconds", 60)),
        user_agent=str(raw.get("user_agent", "")),
        locale=str(raw.get("locale", "fr-FR")),
        source_path=path,
    )
    recipe.check_authorization()
    recipe.assert_url_allowed(recipe.start_url)
    return recipe
