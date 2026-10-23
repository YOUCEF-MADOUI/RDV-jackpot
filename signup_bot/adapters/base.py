"""Interface commune aux « moteurs » d'exécution.

Deux implémentations :
  - PlaywrightAdapter : vrai navigateur (Chromium/Firefox/WebKit) ;
  - MockAdapter       : sans navigateur, pour tester la logique et faire un dry-run.

Le reste du code (runner) ne connaît que cette interface : on peut donc ajouter
demain un adaptateur API (quand le site expose une API officielle), un adaptateur
mobile (Appium) ou un adaptateur « formulaire interne », sans toucher au runner.

Règle importante : AUCUN de ces adaptateurs ne résout de CAPTCHA et ne modifie
l'empreinte du navigateur. Les blocages se traitent par une pause humaine
(pause_for_human) ou via une API officielle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class AdapterError(RuntimeError):
    """Erreur d'exécution côté navigateur."""


class Adapter(ABC):
    """Contrat minimal attendu par le runner."""

    @abstractmethod
    def open(self, url: str) -> None: ...

    @abstractmethod
    def fill(self, selector: str, value: str, timeout_ms: int) -> None: ...

    @abstractmethod
    def select(self, selector: str, value: str, timeout_ms: int) -> None: ...

    @abstractmethod
    def check(self, selector: str, timeout_ms: int) -> None: ...

    @abstractmethod
    def upload(self, selector: str, path: Path, timeout_ms: int) -> None: ...

    @abstractmethod
    def click(self, selector: str, timeout_ms: int) -> None: ...

    @abstractmethod
    def press(self, selector: str, key: str, timeout_ms: int) -> None: ...

    @abstractmethod
    def wait_for(self, selector: str, timeout_ms: int) -> None: ...

    @abstractmethod
    def assert_text(self, text: str, timeout_ms: int) -> None: ...

    def pause_for_human(self, prompt: str) -> None:
        """Point de passage humain (CAPTCHA, OTP, vérification d'identité)."""
        raise AdapterError(
            "pause_for_human non pris en charge par cet adaptateur : utilisez un "
            "navigateur visible (--headful) pour les étapes qui exigent un humain."
        )

    def wait_ms(self, milliseconds: int) -> None:
        import time

        time.sleep(milliseconds / 1000)

    def screenshot(self, path: Path) -> None:
        """Optionnel : capture l'état de la page (utile en cas d'échec)."""

    def close(self) -> None:
        """Libère les ressources. Doit être idempotent."""
