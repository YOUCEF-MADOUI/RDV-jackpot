"""Adaptateur factice : même contrat, aucun navigateur.

Sert à :
  - tester la logique (recettes, rendu, journal) sans dépendance externe ;
  - faire un « dry-run » réaliste avant d'ouvrir un vrai navigateur ;
  - écrire des tests d'intégration reproductibles en CI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Adapter, AdapterError


class MockAdapter(Adapter):
    def __init__(self, *, contact_form_visible: bool = True, fail_on: str = ""):
        self.calls: list[dict[str, Any]] = []
        self.visited: list[str] = []
        self.closed = False
        self.human_pauses: list[str] = []
        self._fail_on = fail_on
        self._texts = [
            "Inscription confirmée" if contact_form_visible else "Formulaire indisponible",
            "Bienvenue",
        ]

    def _record(self, action: str, **kwargs: Any) -> None:
        if self._fail_on and self._fail_on == action:
            raise AdapterError(f"échec simulé sur l'action {action!r}")
        self.calls.append({"action": action, **kwargs})

    def open(self, url: str) -> None:
        self._record("goto", url=url)
        self.visited.append(url)

    def fill(self, selector: str, value: str, timeout_ms: int) -> None:
        self._record("fill", selector=selector, value=value)

    def select(self, selector: str, value: str, timeout_ms: int) -> None:
        self._record("select", selector=selector, value=value)

    def check(self, selector: str, timeout_ms: int) -> None:
        self._record("check", selector=selector)

    def upload(self, selector: str, path: Path, timeout_ms: int) -> None:
        self._record("upload", selector=selector, path=str(path))

    def click(self, selector: str, timeout_ms: int) -> None:
        self._record("click", selector=selector)

    def press(self, selector: str, key: str, timeout_ms: int) -> None:
        self._record("press", selector=selector, key=key)

    def wait_for(self, selector: str, timeout_ms: int) -> None:
        self._record("wait_for", selector=selector)

    def assert_text(self, text: str, timeout_ms: int) -> None:
        if text not in self._texts:
            raise AdapterError(f"texte attendu absent de la page simulée : {text!r}")
        self._record("assert_text", text=text)

    def pause_for_human(self, prompt: str) -> None:
        self.human_pauses.append(prompt)
        self._record("pause_for_human", prompt=prompt)

    def screenshot(self, path: Path) -> None:
        self.calls.append({"action": "screenshot", "path": str(path)})

    def close(self) -> None:
        self.closed = True
