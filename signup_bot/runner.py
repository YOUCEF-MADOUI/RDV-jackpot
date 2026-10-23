"""Orchestrateur : exécute une recette pour un profil, avec journal et artefacts.

Ce module ne sait pas comment fonctionne un site : il déroule des étapes déclarées
dans la recette et délègue au navigateur. Toute la logique « site » reste dans
recipes/*.json, ce qui facilite les tests et la maintenance.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters.base import Adapter, AdapterError
from .audit import AuditLog
from .errors import AutomationError
from .guardrails import RateLimiter
from .models import Profile, ProfileError
from .recipes import Recipe, RecipeError, Step, render


@dataclass
class RunResult:
    run_id: str
    status: str  # "success" | "failed"
    completed_steps: int
    duration_seconds: float
    artifacts_dir: Path
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "success"


class Runner:
    def __init__(
        self,
        *,
        adapter: Adapter,
        recipe: Recipe,
        profile: Profile,
        audit: AuditLog,
        artifacts_dir: Path,
        dry_run: bool = False,
        enforce_rate_limit: bool = True,
        run_id: str | None = None,
    ):
        self.adapter = adapter
        self.recipe = recipe
        self.profile = profile
        self.audit = audit
        self.artifacts_dir = artifacts_dir
        self.dry_run = dry_run
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
        self.mapping = profile.as_mapping()
        self.rate_limiter = (
            RateLimiter(
                state_path=artifacts_dir.parent / ".state" / "rate-limit.json",
                min_interval_seconds=recipe.min_interval_seconds,
            )
            if enforce_rate_limit
            else None
        )

    # ------------------------------------------------------------------ public
    def run(self) -> RunResult:
        started = time.monotonic()
        completed = 0
        run_dir = self.artifacts_dir / self.run_id

        try:
            self.recipe.check_authorization()
            self.recipe.assert_url_allowed(self.recipe.start_url)
            needs_secret = any(step.secret_env for step in self.recipe.steps)
            self.profile.validate(need_password=needs_secret and not self.dry_run)
            if not self.dry_run and self.rate_limiter is not None:
                self.rate_limiter.check(f"{self.profile.profile_id}:{self.recipe.site}")
            if run_dir.exists() and any(run_dir.iterdir()):
                raise AutomationError(
                    f"le dossier d'artefacts {run_dir} existe déjà : exécution déjà effectuée ?"
                )
            run_dir.mkdir(parents=True, exist_ok=True)
            self.audit.event(
                "run_started",
                run_id=self.run_id,
                site=self.recipe.site,
                profile_id=self.profile.profile_id,
                dry_run=self.dry_run,
                steps=len(self.recipe.steps),
            )

            self._open_browser_if_needed()
            self.adapter.open(self.recipe.start_url)

            for index, step in enumerate(self.recipe.steps, start=1):
                self._execute(step, index)
                completed = index

            if self.recipe.success_text:
                self.adapter.assert_text(self.recipe.success_text, 20_000)

            if self.rate_limiter is not None and not self.dry_run:
                self.rate_limiter.mark(f"{self.profile.profile_id}:{self.recipe.site}")

            duration = time.monotonic() - started
            self.audit.event(
                "run_succeeded",
                run_id=self.run_id,
                site=self.recipe.site,
                profile_id=self.profile.profile_id,
                steps=completed,
                duration_s=round(duration, 2),
            )
            return RunResult(
                run_id=self.run_id,
                status="success",
                completed_steps=completed,
                duration_seconds=duration,
                artifacts_dir=run_dir,
                events=self.audit.read(),
            )

        except (ProfileError, RecipeError, AdapterError, AutomationError, FileExistsError) as exc:
            duration = time.monotonic() - started
            message = str(exc)
            self.audit.event(
                "run_failed",
                run_id=self.run_id,
                site=self.recipe.site,
                profile_id=self.profile.profile_id,
                step=completed + 1,
                error=message,
            )
            self._save_failure_screenshot(run_dir)
            return RunResult(
                run_id=self.run_id,
                status="failed",
                completed_steps=completed,
                duration_seconds=duration,
                artifacts_dir=run_dir,
                error=message,
                events=self.audit.read(),
            )
        except Exception as exc:  # noqa: BLE001 - on journalise tout échec inattendu
            duration = time.monotonic() - started
            self.audit.event(
                "run_failed",
                run_id=self.run_id,
                site=self.recipe.site,
                profile_id=self.profile.profile_id,
                step=completed + 1,
                error=f"{type(exc).__name__}: {exc}",
            )
            self._save_failure_screenshot(run_dir)
            return RunResult(
                run_id=self.run_id,
                status="failed",
                completed_steps=completed,
                duration_seconds=duration,
                artifacts_dir=run_dir,
                error=f"{type(exc).__name__}: {exc}",
                events=self.audit.read(),
            )
        finally:
            try:
                self.adapter.close()
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------------- interne
    def _open_browser_if_needed(self) -> None:
        starter = getattr(self.adapter, "start", None)
        if callable(starter):
            starter()

    def _save_failure_screenshot(self, run_dir: Path) -> None:
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            self.adapter.screenshot(run_dir / "failure.png")
        except Exception:  # noqa: BLE001 - jamais bloquant
            pass

    def _execute(self, step: Step, index: int) -> None:
        action = step.action
        value = render(step.value, self.mapping) if step.value else ""
        if step.secret_env:
            value = os.environ.get(step.secret_env, "")
            if not value:
                if not self.dry_run:
                    raise AutomationError(
                        f"secret absent : la variable d'environnement "
                        f"{step.secret_env!r} est vide"
                    )
                # en simulation, on n'exige ni ne révèle aucun secret
                value = "***"
        self.audit.event(
            "step",
            run_id=self.run_id,
            index=index,
            action=action,
            selector=step.selector,
            value="***" if step.secret_env else value,
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
            self.adapter.pause_for_human(prompt)
        elif action == "wait_ms":
            self.adapter.wait_ms(int(value))
        else:  # pragma: no cover - déjà bloqué par la validation de recette
            raise AutomationError(f"action non gérée : {action!r}")
