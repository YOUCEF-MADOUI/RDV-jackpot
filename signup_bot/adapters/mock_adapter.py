"""Adaptateur factice : même contrat, aucun navigateur.

Sert à :
  - tester la logique (recettes, rendu, journal, boucle) sans dépendance externe ;
  - faire un « dry-run » réaliste avant d'ouvrir un vrai navigateur ;
  - écrire des tests d'intégration reproductibles en CI.

`scenario` permet de simuler une page qui évolue : chaque chargement de page
(`open`) ou clic consomme l'état suivant de la liste. Un état = les textes
visibles, les sélecteurs visibles et l'URL courante.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from .base import Adapter, AdapterError

DEFAULT_STATE: dict[str, Any] = {
    "url": "http://127.0.0.1:8000/",
    "texts": ["Inscription confirmée", "Bienvenue"],
    "selectors": ["#first_name"],
}


class MockAdapter(Adapter):
    def __init__(
        self,
        *,
        scenario: Sequence[dict[str, Any]] | None = None,
        fail_on: str = "",
        page_texts: Sequence[str] | None = None,
        visible_selectors: Sequence[str] | None = None,
        url: str = "",
        assume_present: bool = False,
    ):
        self.calls: list[dict[str, Any]] = []
        self.visited: list[str] = []
        self.closed = False
        self.human_pauses: list[str] = []
        self.saved_sessions: list[str] = []
        self.loaded_session: str | None = None
        self._fail_on = fail_on
        self._assume_present = assume_present
        self._scenario = list(scenario or [])
        self._index = 0
        first = dict(self._scenario[0]) if self._scenario else dict(DEFAULT_STATE)
        if page_texts is not None:
            first["texts"] = list(page_texts)
        if visible_selectors is not None:
            first["selectors"] = list(visible_selectors)
        if url:
            first["url"] = url
        self._state = first
        self.url_value = ""
        self._apply(first)

    # ------------------------------------------------------------------ état
    def _apply(self, state: dict[str, Any]) -> None:
        self.page_texts = [str(t) for t in state.get("texts", [])]
        self.visible_selectors = [str(s) for s in state.get("selectors", [])]
        if state.get("url"):
            self.url_value = str(state["url"])

    def _advance(self) -> None:
        if self._index + 1 < len(self._scenario):
            self._index += 1
            self._state = dict(self._scenario[self._index])
            self._apply(self._state)

    def set_page(self, *, texts: Sequence[str] | None = None,
                  selectors: Sequence[str] | None = None, url: str = "") -> None:
        """Change l'état courant à la main (utile dans un test)."""
        if texts is not None:
            self.page_texts = list(texts)
        if selectors is not None:
            self.visible_selectors = list(selectors)
        if url:
            self.url_value = url

    def _record(self, action: str, **kwargs: Any) -> None:
        if self._fail_on and self._fail_on == action:
            raise AdapterError(f"échec simulé sur l'action {action!r}")
        self.calls.append({"action": action, **kwargs})

    def count(self, action: str) -> int:
        return sum(1 for call in self.calls if call["action"] == action)

    # ------------------------------------------------------------ navigation
    def open(self, url: str) -> None:
        self._record("goto", url=url)
        self.visited.append(url)
        self.url_value = url
        self._advance()

    def current_url(self) -> str:
        return self.url_value

    def navigate_back(self) -> None:
        self._record("back")
        self._advance()

    # --------------------------------------------------------------- saisie
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
        self._advance()

    def press(self, selector: str, key: str, timeout_ms: int) -> None:
        self._record("press", selector=selector, key=key)
        self._advance()

    def wait_for(self, selector: str, timeout_ms: int) -> None:
        self._record("wait_for", selector=selector)

    def assert_text(self, text: str, timeout_ms: int) -> None:
        if text not in self.page_texts:
            raise AdapterError(f"texte attendu absent de la page simulée : {text!r}")
        self._record("assert_text", text=text)

    # ----------------------------------------------------------- observation
    def text_present(self, text: str, timeout_ms: int) -> bool:
        if self._assume_present:
            self._record("text_present", text=text, found=True)
            return True
        found = any(text.lower() in candidate.lower() for candidate in self.page_texts)
        self._record("text_present", text=text, found=found)
        return found

    def selector_present(self, selector: str, timeout_ms: int) -> bool:
        if self._assume_present:
            self._record("selector_present", selector=selector, found=True)
            return True
        found = selector in self.visible_selectors
        self._record("selector_present", selector=selector, found=found)
        return found

    # ---------------------------------------------------------------- divers
    def pause_for_human(self, prompt: str) -> None:
        self.human_pauses.append(prompt)
        self._record("pause_for_human", prompt=prompt)

    def start(self, storage_state: Path | None = None) -> None:
        self.loaded_session = str(storage_state) if storage_state else None
        self._record("start", storage_state=self.loaded_session)

    def save_storage_state(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        self.saved_sessions.append(str(path))

    def screenshot(self, path: Path) -> None:
        self.calls.append({"action": "screenshot", "path": str(path)})

    def close(self) -> None:
        self.closed = True
