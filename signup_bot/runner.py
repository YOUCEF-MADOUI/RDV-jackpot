"""Orchestrateur : connexion, actions, et boucle « tant que la notification X ».

Ce module ne sait pas comment fonctionne un site : il déroule des étapes déclarées
dans la recette, observe la page à des points précis, et délègue au navigateur.

Déroulement d'un run :

    contrôle recette/autorisation ─► connexion (login) ─► [boucle]
                                                        └─ actions ─► observer
                                                           ├ succès      → fin
                                                           ├ état inattendu → abandon
                                                           └ réessai     → pause puis
                                                                            nouvelle tentative
    (sans boucle : actions puis vérification finale, une seule fois)
"""

from __future__ import annotations

import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .accounts import Account, AccountError
from .adapters.base import Adapter, AdapterError
from .audit import AuditLog
from .conditions import Condition
from .errors import AutomationError
from .guardrails import RateLimiter
from .models import Profile, ProfileError
from .notify import Notifier
from .recipes import Recipe, RecipeError, Step, render

OUTCOME_SUCCESS = "success"
OUTCOME_STOPPED = "stopped"  # condition stop_when rencontrée : on s'arrête proprement
OUTCOME_ABANDONED = "abandoned"  # page inattendue, ni succès ni réessai
OUTCOME_MAX_ATTEMPTS = "exhausted"  # plafond d'essais ou de durée atteint
OUTCOME_FAILED = "failed"  # erreur technique


@dataclass
class RunResult:
    run_id: str
    status: str
    completed_steps: int
    duration_seconds: float
    artifacts_dir: Path
    attempts: int = 0
    error: str = ""
    reason: str = ""
    matched: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == OUTCOME_SUCCESS


class Runner:
    def __init__(
        self,
        *,
        adapter: Adapter,
        recipe: Recipe,
        profile: Profile | None = None,
        account: Account | None = None,
        audit: AuditLog,
        artifacts_dir: Path,
        dry_run: bool = False,
        enforce_rate_limit: bool = True,
        run_id: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        notifier: Notifier | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ):
        self.adapter = adapter
        self.recipe = recipe
        self.profile = profile
        self.account = account
        self.audit = audit
        self.artifacts_dir = Path(artifacts_dir)
        self.dry_run = dry_run
        self.run_id = run_id or (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
        )
        self.sleep = sleep
        self.clock = clock
        self.rng = rng or random.Random()
        self.notifier = notifier
        self.stop_requested = stop_requested or (lambda: False)
        self.mapping: dict[str, str] = {}
        if account is not None:
            self.mapping.update(account.as_mapping())
        if profile is not None:
            self.mapping.update(profile.as_mapping())
        self.rate_limiter = (
            RateLimiter(
                state_path=self.artifacts_dir.parent / ".state" / "rate-limit.json",
                min_interval_seconds=recipe.min_interval_seconds,
            )
            if enforce_rate_limit
            else None
        )
        self._slept_seconds = 0.0

    @property
    def slept_seconds(self) -> float:
        return self._slept_seconds

    # ------------------------------------------------------------------ public
    def run(self) -> RunResult:
        started = self.clock()
        run_dir = self.artifacts_dir / self.run_id
        state: dict[str, Any] = {"completed": 0, "attempts": 0, "matched": []}

        try:
            self._preflight(run_dir)
            self._start_adapter()
            self.audit.event(
                "run_started",
                run_id=self.run_id,
                site=self.recipe.site,
                profile_id=self.profile.profile_id if self.profile else "",
                account_id=self.account.account_id if self.account else "",
                dry_run=self.dry_run,
                mode="loop" if self.recipe.loop else "single",
            )

            if self.recipe.login is not None:
                self._login(state)

            self.adapter.open(self.recipe.start_url)
            loop_spec = self.recipe.effective_loop()

            loop = loop_spec
            if loop is not None:
                outcome = self._run_loop(loop, run_dir, state)
            else:
                outcome = self._run_single(run_dir, state)

            duration = self.clock() - started
            self.audit.event(
                "run_finished",
                run_id=self.run_id,
                site=self.recipe.site,
                status=outcome["status"],
                attempts=state["attempts"],
                steps=state["completed"],
                reason=outcome.get("reason", ""),
                duration_s=round(duration, 2),
            )
            self._notify(outcome, state)
            return RunResult(
                run_id=self.run_id,
                status=outcome["status"],
                completed_steps=state["completed"],
                duration_seconds=duration,
                artifacts_dir=run_dir,
                attempts=state["attempts"],
                error=outcome.get("error", ""),
                reason=outcome.get("reason", ""),
                matched=state["matched"],
                events=self.audit.read(),
            )

        except (ProfileError, AccountError, RecipeError, AdapterError, AutomationError) as exc:
            return self._fail(started, run_dir, state, str(exc))
        except FileExistsError as exc:
            return self._fail(started, run_dir, state, str(exc))
        except Exception as exc:  # noqa: BLE001 - tout échec inattendu est journalisé
            return self._fail(started, run_dir, state, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                self.adapter.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ étapes
    def _preflight(self, run_dir: Path) -> None:
        self.recipe.check_authorization()
        self.recipe.assert_url_allowed(self.recipe.start_url)
        if not self.recipe.steps and self.recipe.loop is None:
            raise RecipeError("aucune action à exécuter : 'steps' et 'loop' sont vides")

        if self.profile is not None:
            self.profile.validate(need_password=False)
        if self.account is not None:
            self.account.validate(need_password=False)

        # Les secrets sont vérifiés AVANT d'ouvrir un navigateur : inutile de se
        # connecter pour découvrir que le mot de passe n'est pas disponible.
        if not self.dry_run:
            self._assert_secrets_available()

        if not self.dry_run and self.rate_limiter is not None:
            key = f"{self._actor()}:{self.recipe.site}"
            self.rate_limiter.check(key)
            self.rate_limiter.mark(key)

        if run_dir.exists() and any(run_dir.iterdir()):
            raise AutomationError(
                f"le dossier d'artefacts {run_dir} existe déjà : exécution déjà effectuée ?"
            )
        run_dir.mkdir(parents=True, exist_ok=True)

    def _actor(self) -> str:
        if self.account is not None:
            return f"account:{self.account.account_id}"
        if self.profile is not None:
            return f"profile:{self.profile.profile_id}"
        return "anonymous"

    def _assert_secrets_available(self) -> None:
        for name in sorted(self.recipe.all_secret_envs()):
            if not os.environ.get(name):
                raise AutomationError(
                    f"secret absent : la variable d'environnement {name!r} est vide."
                )
        if self.account is not None and self.recipe.login is not None:
            self.account.password()  # lève AccountError si la variable est vide
        for step_pair in self._all_steps():
            for step in step_pair:
                if step.secret_field and self.account is None:
                    raise AutomationError(
                        f"étape {step.describe()} : 'secret_field' exige un fichier de "
                        "compte (--account)."
                    )

    def _all_steps(self) -> list[tuple[Step, ...]]:
        groups = [self.recipe.steps]
        if self.recipe.login is not None:
            groups.append(self.recipe.login.steps)
        if self.recipe.loop is not None:
            groups.extend([self.recipe.loop.steps, self.recipe.loop.reset_steps])
        return groups

    def _start_adapter(self) -> None:
        state_path = None
        login = self.recipe.login
        if login is not None and login.reuse_session:
            state_path = self._session_path()
            if not state_path.is_file():
                state_path = None
        starter = getattr(self.adapter, "start", None)
        if callable(starter):
            try:
                starter(state_path)
            except TypeError:  # adaptateur sans paramètre storage_state
                starter()

    def _session_path(self) -> Path:
        assert self.recipe.login is not None
        account_id = self.account.account_id if self.account else "anonyme"
        return (
            self.artifacts_dir.parent
            / self.recipe.session_dir.replace("artifacts/", "", 1)
            / f"{self.recipe.site}-{account_id}.json"
        ).resolve()

    # ------------------------------------------------------------------- login
    def _login(self, state: dict[str, Any]) -> None:
        login = self.recipe.login
        assert login is not None
        self.recipe.assert_url_allowed(login.url)

        already = self._logged_in(login.logged_in_when, quiet=True)
        if already:
            self.audit.event("login_reused", run_id=self.run_id, url=login.url)
            return

        self.audit.event("login_started", run_id=self.run_id, url=login.url)
        self.adapter.open(login.url)
        completed = self._execute_steps(login.steps, state, where="login")

        if login.success_when:
            matched = self._wait_for_conditions(login.success_when, timeout_s=30)
            if not matched:
                raise AutomationError(
                    "connexion échouée : aucune des conditions login.success_when "
                    f"n'est apparue ({self._labels(login.success_when)})"
                )
            self.audit.event("login_succeeded", run_id=self.run_id, signal=matched)
        else:
            self.audit.event("login_steps_done", run_id=self.run_id, steps=completed)

        if not self.dry_run:
            try:
                self.adapter.save_storage_state(self._session_path())
            except Exception:  # noqa: BLE001 - la session est un confort, pas une exigence
                pass

    def _logged_in(self, conditions: Sequence[Condition], *, quiet: bool = False) -> bool:
        if not conditions:
            return False
        matched = self._match_any(conditions, timeout_s=3 if quiet else 10)
        return bool(matched)

    # -------------------------------------------------------------- mode simple
    def _run_single(self, run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
        self._execute_steps(self.recipe.steps, state, where="steps")
        if self.recipe.success_text:
            self.adapter.assert_text(self.recipe.success_text, 20_000)
            state["matched"].append(f"text={self.recipe.success_text!r}")
        state["attempts"] = 1
        return {"status": OUTCOME_SUCCESS, "reason": "parcours terminé"}

    # ------------------------------------------------------------------ boucle
    def _run_loop(self, loop, run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
        deadline = self.clock() + loop.max_duration_seconds
        interval = float(loop.interval_seconds)
        attempt = 0
        last_reason = ""

        while attempt < loop.max_attempts:
            if self.stop_requested():
                self.audit.event("stop_requested", run_id=self.run_id)
                return {
                    "status": OUTCOME_STOPPED,
                    "reason": "arrêt demandé par l'utilisateur",
                }
            if self.clock() > deadline:
                return {
                    "status": OUTCOME_MAX_ATTEMPTS,
                    "reason": f"durée maximale atteinte ({loop.max_duration_seconds} s)",
                    "error": last_reason,
                }

            attempt += 1
            state["attempts"] = attempt
            self.audit.event("loop_attempt", run_id=self.run_id, attempt=attempt,
                             max_attempts=loop.max_attempts)

            # 1. Peut-être que le résultat est déjà là (ex. la page a été rechargée).
            if loop.evaluate_before_steps and attempt == 1:
                early = self._observe(loop, state, lenient=True)
                if early is not None:
                    return early

            # 2. Retour à un état connu, puis actions.
            self._execute_steps(loop.reset_steps, state, where="reset")
            self._execute_steps(loop.steps, state, where="loop")

            # 3. Lecture du résultat.
            outcome = self._observe(loop, state)
            if outcome is not None:
                return outcome

            last_reason = "condition de réessai présente"
            if attempt >= loop.max_attempts:
                break

            # 4. Pause polie : intervalle + jitter, avec back-off progressif.
            wait = min(interval, loop.max_interval_seconds)
            if loop.jitter_seconds:
                wait += self.rng.uniform(0, loop.jitter_seconds)
            if self.dry_run:
                wait = min(wait, 1.0)
            self.audit.event(
                "sleep", run_id=self.run_id, seconds=round(wait, 1), attempt=attempt
            )
            self._slept_seconds += wait
            self.sleep(wait)
            interval = min(interval * loop.backoff_factor, loop.max_interval_seconds)

        if self.notifier is not None:
            self.notifier.send(
                "exhausted",
                {
                    "run_id": self.run_id,
                    "site": self.recipe.site,
                    "account": self.account.account_id if self.account else "",
                    "attempts": attempt,
                    "reason": last_reason,
                },
            )
        return {
            "status": OUTCOME_MAX_ATTEMPTS,
            "reason": f"plafond d'essais atteint ({loop.max_attempts})",
            "error": last_reason,
        }

    def _observe(
        self, loop, state: dict[str, Any], *, lenient: bool = False
    ) -> dict[str, Any] | None:
        """Applique l'ordre de priorité : succès → arrêt → réessai → inattendu.

        `lenient=True` (pré-contrôle avant les actions) : si rien ne correspond, on
        continue le parcours au lieu de conclure à un état inattendu.
        """
        success = self._match_any(loop.success_when, timeout_s=8) if loop.success_when else ""
        if success:
            self._record_signal(state, success)
            self.audit.event("loop_success", run_id=self.run_id, signal=success)
            return {"status": OUTCOME_SUCCESS, "reason": f"résultat atteint ({success})"}

        stop = self._match_any(loop.stop_when, timeout_s=3) if loop.stop_when else ""
        if stop:
            self._record_signal(state, stop)
            self.audit.event("loop_stopped", run_id=self.run_id, signal=stop)
            return {"status": OUTCOME_STOPPED, "reason": f"condition d'arrêt ({stop})"}

        retry = self._match_any(loop.retry_when, timeout_s=3) if loop.retry_when else ""
        if retry:
            self._record_signal(state, retry)
            self.audit.event("loop_retry", run_id=self.run_id, signal=retry)
            return None

        if lenient:
            return None
        self.audit.event(
            "loop_unexpected_state",
            run_id=self.run_id,
            expected=self._labels(loop.retry_when),
        )
        return {
            "status": OUTCOME_ABANDONED,
            "reason": "page dans un état inattendu : ni succès, ni arrêt, ni condition de "
            f"réessai ({self._labels(loop.retry_when)})",
        }

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _labels(conditions: Sequence[Condition]) -> str:
        return ", ".join(condition.label() for condition in conditions) or "(aucune)"

    def _record_signal(self, state: dict[str, Any], label: str) -> None:
        """Garde une trace chronologique sans répéter 10 fois la même notification."""
        matched: list[str] = state["matched"]
        if not matched or matched[-1] != label:
            matched.append(label)

    def _match_any(self, conditions: Sequence[Condition], *, timeout_s: float) -> str:
        """Renvoie le libellé de la première condition vraie, sinon chaîne vide."""
        for condition in conditions:
            timeout_ms = min(condition.timeout_ms, int(timeout_s * 1000))
            try:
                if self._with_timeout(condition, timeout_ms).matches(self.adapter):
                    label = condition.label()
                    self.audit.event("condition_matched", run_id=self.run_id, condition=label)
                    return label
            except Exception as exc:  # noqa: BLE001 - une observation ne doit pas planter
                self.audit.event(
                    "condition_error", run_id=self.run_id, condition=condition.label(),
                    error=f"{type(exc).__name__}: {exc}",
                )
        return ""

    def _wait_for_conditions(self, conditions: Sequence[Condition], *, timeout_s: float) -> str:
        """Attend qu'au moins une condition devienne vraie (fin de connexion)."""
        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            matched = self._match_any(conditions, timeout_s=2)
            if matched:
                return matched
            self.sleep(0.5)
            self._slept_seconds += 0.5
        return ""

    @staticmethod
    def _with_timeout(condition: Condition, timeout_ms: int) -> Condition:
        if timeout_ms == condition.timeout_ms:
            return condition
        from dataclasses import replace as _replace

        return _replace(condition, timeout_ms=max(timeout_ms, 0))

    def _execute_steps(
        self, steps: Sequence[Step], state: dict[str, Any], *, where: str
    ) -> int:
        for index, step in enumerate(steps, start=1):
            if self.stop_requested():
                raise AutomationError("arrêt demandé par l'utilisateur")
            self._execute(step, index, where)
            state["completed"] += 1
        return len(steps)

    def _resolve_secret(self, step: Step) -> str:
        if step.secret_env:
            value = os.environ.get(step.secret_env, "")
            if not value:
                if self.dry_run:
                    return "***"
                raise AutomationError(
                    f"secret absent : la variable d'environnement {step.secret_env!r} est vide"
                )
            return value
        if step.secret_field:
            if self.account is None:
                raise AutomationError("'secret_field' exige un fichier de compte")
            if step.secret_field == "username":
                return self.account.username
            if self.dry_run and not os.environ.get(self.account.password_env):
                return "***"
            return self.account.password()
        return ""

    def _execute(self, step: Step, index: int, where: str) -> None:
        action = step.action
        value = render(step.value, self.mapping) if step.value else ""
        if step.is_secret():
            value = self._resolve_secret(step)
        self.audit.event(
            "step",
            run_id=self.run_id,
            scope=where,
            index=index,
            action=action,
            selector=step.selector,
            value="***" if step.is_secret() else value,
        )

        if action == "goto":
            self.recipe.assert_url_allowed(value)
            self.adapter.open(value)
        elif action == "fill":
            self.adapter.fill(step.selector, value, step.timeout_ms)
        elif action == "select":
            self.adapter.select(step.selector, value, step.timeout_ms)
        elif action == "check":
            self.adapter.check(step.selector, step.timeout_ms)
        elif action == "upload":
            path = Path(value)
            if not path.is_file():
                raise AutomationError(f"fichier à envoyer introuvable : {path}")
            self.adapter.upload(step.selector, path, step.timeout_ms)
        elif action == "click":
            self.adapter.click(step.selector, step.timeout_ms)
        elif action == "press":
            self.adapter.press(step.selector, value, step.timeout_ms)
        elif action == "wait_for":
            self.adapter.wait_for(step.selector, step.timeout_ms)
        elif action == "assert_text":
            self.adapter.assert_text(step.contains, step.timeout_ms)
        elif action == "pause_for_human":
            prompt = step.prompt or "Action requise dans le navigateur (CAPTCHA, code reçu…)"
            self.audit.event("human_pause", run_id=self.run_id, prompt=prompt)
            self.adapter.pause_for_human(prompt)
        elif action == "wait_ms":
            self.adapter.wait_ms(int(value))
        else:  # pragma: no cover - déjà bloqué par la validation de recette
            raise AutomationError(f"action non gérée : {action!r}")

    # ------------------------------------------------------------------ échec
    def _fail(
        self, started: float, run_dir: Path, state: dict[str, Any], message: str
    ) -> RunResult:
        duration = self.clock() - started
        self.audit.event(
            "run_failed",
            run_id=self.run_id,
            site=self.recipe.site,
            account_id=self.account.account_id if self.account else "",
            profile_id=self.profile.profile_id if self.profile else "",
            step=state["completed"] + 1,
            error=message,
        )
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            self.adapter.screenshot(run_dir / "failure.png")
        except Exception:  # noqa: BLE001 - jamais bloquant
            pass
        if self.notifier is not None:
            self.notifier.send(
                "failed",
                {
                    "run_id": self.run_id,
                    "site": self.recipe.site,
                    "account": self.account.account_id if self.account else "",
                    "attempts": state["attempts"],
                    "error": message,
                },
            )
        return RunResult(
            run_id=self.run_id,
            status=OUTCOME_FAILED,
            completed_steps=state["completed"],
            duration_seconds=duration,
            artifacts_dir=run_dir,
            attempts=state["attempts"],
            error=message,
            matched=state["matched"],
            events=self.audit.read(),
        )

    def _notify(self, outcome: dict[str, Any], state: dict[str, Any]) -> None:
        if self.notifier is None:
            return
        self.notifier.send(
            outcome["status"],
            {
                "run_id": self.run_id,
                "site": self.recipe.site,
                "account": self.account.account_id if self.account else "",
                "profile": self.profile.profile_id if self.profile else "",
                "attempts": state["attempts"],
                "reason": outcome.get("reason", ""),
            },
        )
