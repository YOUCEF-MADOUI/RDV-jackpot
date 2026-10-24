"""« Recettes » : description déclarative du parcours d'un site.

Trois blocs, tous optionnels sauf `steps` :

  login  connexion au compte (identifiant/mot de passe du propriétaire)
  steps  actions à faire une fois connecté (inscription, formulaire…)
  loop   boucle de surveillance : on refait les actions tant que la condition
         de réessai est vraie, jusqu'au succès (ou abandon)

Ajouter un site = ajouter un fichier JSON, jamais un nouveau programme.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .conditions import Condition, ConditionError, condition_from

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
SECRET_FIELDS = {"password", "username"}

TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

# Plancher de politesse : jamais moins d'un essai toutes les 15 s sur un site
# distant. Une exception n'est acceptée que pour les hôtes de test locaux.
POLL_FLOOR_SECONDS = 15
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


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
    secret_field: str = ""
    timeout_ms: int = 15_000

    def describe(self) -> str:
        target = self.selector or self.value or self.contains or ""
        marker = " [secret]" if self.is_secret() else ""
        return f"{self.action}({target}){marker}"

    def is_secret(self) -> bool:
        return bool(self.secret_env or self.secret_field)


def _step_from(raw: Mapping[str, Any], index: int) -> Step:
    where = f"étape {index + 1}"
    try:
        action = str(raw["action"]).strip()
    except KeyError as exc:
        raise RecipeError(f"{where} : champ 'action' manquant") from exc
    if action not in SUPPORTED_ACTIONS:
        raise RecipeError(
            f"{where} : action inconnue {action!r} "
            f"(supportées : {', '.join(sorted(SUPPORTED_ACTIONS))})"
        )
    selector = str(raw.get("selector", ""))
    value = str(raw.get("value", ""))
    secret_env = str(raw.get("secret_env", ""))
    secret_field = str(raw.get("secret_field", ""))
    if action in ACTIONS_REQUIRING_SELECTOR and not selector:
        raise RecipeError(f"{where} ({action}) : 'selector' obligatoire")
    if action in ACTIONS_REQUIRING_VALUE and value == "" and not (secret_env or secret_field):
        raise RecipeError(
            f"{where} ({action}) : 'value' (ou 'secret_env'/'secret_field') obligatoire"
        )
    if (secret_env or secret_field) and action not in ("fill", "select"):
        raise RecipeError(f"{where} : un secret ne peut remplir que fill/select")
    if secret_env and secret_field:
        raise RecipeError(f"{where} : 'secret_env' et 'secret_field' sont exclusifs")
    if secret_field and secret_field not in SECRET_FIELDS:
        raise RecipeError(
            f"{where} : 'secret_field' doit valoir "
            f"{' ou '.join(sorted(SECRET_FIELDS))}"
        )
    if action == "assert_text" and not raw.get("contains"):
        raise RecipeError(f"{where} (assert_text) : 'contains' obligatoire")
    return Step(
        action=action,
        selector=selector,
        value=value,
        contains=str(raw.get("contains", "")),
        prompt=str(raw.get("prompt", "")),
        secret_env=secret_env,
        secret_field=secret_field,
        timeout_ms=int(raw.get("timeout_ms", 15_000)),
    )


def _steps_from(raw: Any, where: str) -> tuple[Step, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RecipeError(f"{where} : doit être une liste d'étapes")
    return tuple(_step_from(step, i) for i, step in enumerate(raw))


def _conditions_from(raw: Any, where: str) -> tuple[Condition, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RecipeError(f"{where} : doit être une liste de conditions")
    try:
        return tuple(condition_from(item, f"{where}[{i}]") for i, item in enumerate(raw))
    except ConditionError as exc:
        raise RecipeError(str(exc)) from exc


@dataclass(frozen=True)
class LoginSpec:
    url: str
    steps: tuple[Step, ...]
    success_when: tuple[Condition, ...] = ()
    logged_in_when: tuple[Condition, ...] = ()
    reuse_session: bool = True

    def check(self, allowed_hosts: tuple[str, ...]) -> None:
        assert_host_allowed(self.url, allowed_hosts)
        if not self.steps and not self.logged_in_when:
            raise RecipeError(
                "login : fournissez soit des 'steps', soit un 'logged_in_when' "
                "permettant de réutiliser une session existante"
            )


@dataclass(frozen=True)
class LoopSpec:
    steps: tuple[Step, ...]
    retry_when: tuple[Condition, ...]
    success_when: tuple[Condition, ...]
    stop_when: tuple[Condition, ...] = ()
    reset_steps: tuple[Step, ...] = ()
    evaluate_before_steps: bool = True
    max_attempts: int = 20
    max_duration_seconds: int = 1800
    interval_seconds: float = 45.0
    max_interval_seconds: float = 300.0
    backoff_factor: float = 1.3
    jitter_seconds: float = 10.0
    url: str = ""

    def floor_seconds(self, local_hosts: bool) -> float:
        return 1.0 if local_hosts else POLL_FLOOR_SECONDS


@dataclass(frozen=True)
class Recipe:
    site: str
    start_url: str
    allowed_hosts: tuple[str, ...]
    authorized: bool
    authorization_note: str = ""
    steps: tuple[Step, ...] = field(default_factory=tuple)
    success_text: str = ""
    login: LoginSpec | None = None
    loop: LoopSpec | None = None
    notify: tuple[Mapping[str, Any], ...] = ()
    session_dir: str = "artifacts/.state/sessions"
    min_interval_seconds: int = 60
    user_agent: str = ""
    locale: str = "fr-FR"
    source_path: Path | None = None

    # ------------------------------------------------------------- garde-fous
    def assert_url_allowed(self, url: str) -> None:
        assert_host_allowed(url, self.allowed_hosts)

    def is_local(self) -> bool:
        return all(
            host.lower().lstrip("*.") in LOOPBACK_HOSTS for host in self.allowed_hosts
        )

    def effective_loop(self) -> LoopSpec | None:
        """Applique le plancher de politesse et le jitter par défaut."""
        if self.loop is None:
            return None
        floor = 1.0 if self.is_local() else POLL_FLOOR_SECONDS
        interval = max(float(self.loop.interval_seconds), floor)
        if interval != self.loop.interval_seconds:
            from dataclasses import replace as _replace

            return _replace(self.loop, interval_seconds=interval)
        return self.loop

    def requires_human_pause(self) -> bool:
        steps = list(self.steps)
        if self.loop is not None:
            steps += list(self.loop.steps) + list(self.loop.reset_steps)
        if self.login is not None:
            steps += list(self.login.steps)
        return any(step.action == "pause_for_human" for step in steps)

    def all_secret_envs(self) -> set[str]:
        steps = list(self.steps)
        if self.loop is not None:
            steps += list(self.loop.steps) + list(self.loop.reset_steps)
        if self.login is not None:
            steps += list(self.login.steps)
        return {step.secret_env for step in steps if step.secret_env}

    def check_authorization(self) -> None:
        if not self.authorized:
            raise RecipeError(
                "recette non autorisée : renseignez \"authorized\": true uniquement si "
                "vous êtes propriétaire du site ou disposez d'une autorisation écrite. "
                "Ce projet ne doit pas servir à créer des comptes ou réserver en masse "
                "sur un service tiers."
            )

    def check_consistency(self) -> None:
        """Cohérence d'ensemble : au moins un parcours, boucle complète."""
        if not self.steps and self.loop is None:
            raise RecipeError("la recette doit contenir 'steps' ou 'loop'")
        if self.loop is not None:
            if not self.loop.success_when:
                raise RecipeError("loop : 'success_when' obligatoire (sinon on ne sait pas quand s'arrêter)")
            if not self.loop.retry_when:
                raise RecipeError(
                    "loop : 'retry_when' obligatoire (la notification qui déclenche un nouvel essai)"
                )
            if self.loop.max_attempts < 1:
                raise RecipeError("loop : max_attempts doit être >= 1")
            if self.loop.max_attempts > 500:
                raise RecipeError("loop : max_attempts plafonné à 500 (politesse)")
            if self.loop.max_duration_seconds < 30:
                raise RecipeError("loop : max_duration_seconds doit être >= 30")
            if not 1.0 <= self.loop.backoff_factor <= 5.0:
                raise RecipeError("loop : backoff_factor doit être entre 1.0 et 5.0")
            if not 0 <= self.loop.jitter_seconds <= 120:
                raise RecipeError("loop : jitter_seconds doit être entre 0 et 120")
            if self.loop.max_interval_seconds < self.loop.interval_seconds:
                raise RecipeError(
                    "loop : max_interval_seconds doit être >= interval_seconds"
                )
        if self.login is not None:
            self.login.check(self.allowed_hosts)


def assert_host_allowed(url: str, allowed_hosts: tuple[str, ...]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RecipeError(f"schéma d'URL refusé : {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    for allowed in allowed_hosts:
        allowed = allowed.lower()
        if host == allowed or (allowed.startswith("*.") and host.endswith(allowed[1:])):
            return
    raise RecipeError(
        f"hôte non autorisé : {host!r}. Domaines permis par la recette : "
        + ", ".join(allowed_hosts)
    )


def load_recipe(path: str | Path) -> Recipe:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise RecipeError(f"recette introuvable : {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecipeError(f"JSON invalide dans {path.name} : {exc}") from exc
    if not isinstance(raw, Mapping):
        raise RecipeError("une recette doit être un objet JSON")

    for key in ("site", "start_url", "allowed_hosts"):
        if key not in raw:
            raise RecipeError(f"champ obligatoire manquant dans la recette : {key!r}")

    allowed_hosts = tuple(str(h) for h in raw["allowed_hosts"])
    if not allowed_hosts:
        raise RecipeError("allowed_hosts ne peut pas être vide")

    login = None
    if raw.get("login"):
        login_raw = raw["login"]
        if not isinstance(login_raw, Mapping) or not login_raw.get("url"):
            raise RecipeError("login : 'url' obligatoire")
        login = LoginSpec(
            url=str(login_raw["url"]),
            steps=_steps_from(login_raw.get("steps"), "login.steps"),
            success_when=_conditions_from(login_raw.get("success_when"), "login.success_when"),
            logged_in_when=_conditions_from(
                login_raw.get("logged_in_when"), "login.logged_in_when"
            ),
            reuse_session=bool(login_raw.get("reuse_session", True)),
        )

    loop = None
    if raw.get("loop"):
        loop_raw = raw["loop"]
        if not isinstance(loop_raw, Mapping):
            raise RecipeError("loop doit être un objet JSON")
        loop = LoopSpec(
            steps=_steps_from(loop_raw.get("steps"), "loop.steps"),
            reset_steps=_steps_from(loop_raw.get("reset_steps"), "loop.reset_steps"),
            retry_when=_conditions_from(loop_raw.get("retry_when"), "loop.retry_when"),
            success_when=_conditions_from(loop_raw.get("success_when"), "loop.success_when"),
            stop_when=_conditions_from(loop_raw.get("stop_when"), "loop.stop_when"),
            evaluate_before_steps=bool(loop_raw.get("evaluate_before_steps", True)),
            max_attempts=int(loop_raw.get("max_attempts", 20)),
            max_duration_seconds=int(loop_raw.get("max_duration_seconds", 1800)),
            interval_seconds=float(loop_raw.get("interval_seconds", 45)),
            max_interval_seconds=float(loop_raw.get("max_interval_seconds", 300)),
            backoff_factor=float(loop_raw.get("backoff_factor", 1.3)),
            jitter_seconds=float(loop_raw.get("jitter_seconds", 10)),
            url=str(loop_raw.get("url", "")),
        )
        if not loop.reset_steps:
            # Par défaut : on revient à la page de départ avant chaque essai.
            from dataclasses import replace as _replace

            loop = _replace(
                loop,
                reset_steps=(
                    Step(action="goto", value=loop.url or str(raw["start_url"])),
                ),
            )
            recipe_url = str(loop.reset_steps[0].value)
            assert_host_allowed(recipe_url, allowed_hosts)

    notify_raw = raw.get("notify") or []
    if not isinstance(notify_raw, list):
        raise RecipeError("notify doit être une liste")

    recipe = Recipe(
        site=str(raw["site"]),
        start_url=str(raw["start_url"]),
        allowed_hosts=allowed_hosts,
        authorized=bool(raw.get("authorized", False)),
        authorization_note=str(raw.get("authorization_note", "")),
        steps=_steps_from(raw.get("steps"), "steps"),
        success_text=str(raw.get("success_text", "")),
        login=login,
        loop=loop,
        notify=tuple(notify_raw),
        session_dir=str(raw.get("session_dir", "artifacts/.state/sessions")),
        min_interval_seconds=int(raw.get("min_interval_seconds", 60)),
        user_agent=str(raw.get("user_agent", "")),
        locale=str(raw.get("locale", "fr-FR")),
        source_path=path,
    )
    recipe.check_authorization()
    recipe.assert_url_allowed(recipe.start_url)
    recipe.check_consistency()
    # Tout lien présent dans la recette doit rester dans les domaines autorisés.
    for group in (recipe.steps,
                  recipe.login.steps if recipe.login else (),
                  recipe.loop.steps if recipe.loop else (),
                  recipe.loop.reset_steps if recipe.loop else ()):
        for step in group:
            if step.action == "goto" and step.value and "{{" not in step.value:
                recipe.assert_url_allowed(step.value)
    return recipe
