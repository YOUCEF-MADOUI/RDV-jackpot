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

    # ------------------------------------------------------------- navigation
    @abstractmethod
    def open(self, url: str) -> None: ...

    @abstractmethod
    def current_url(self) -> str: ...

    def navigate_back(self) -> None:
        raise AdapterError("navigate_back non pris en charge par cet adaptateur")

    # ---------------------------------------------------------------- saisie
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

    # ------------------------------------------------------------ observation
    @abstractmethod
    def text_present(self, text: str, timeout_ms: int) -> bool:
        """Vrai si ce texte est visible sur la page. N'échoue jamais : répond oui/non."""

    @abstractmethod
    def selector_present(self, selector: str, timeout_ms: int) -> bool:
        """Vrai si au moins un élément correspondant est visible. N'échoue jamais."""

    # ------------------------------------------------------------------ divers
    def pause_for_human(self, prompt: str) -> None:
        """Point de passage humain (CAPTCHA, OTP, vérification d'identité)."""
        raise AdapterError(
            "pause_for_human non pris en charge par cet adaptateur : utilisez un "
            "navigateur visible (--headful) pour les étapes qui exigent un humain."
        )

    def wait_ms(self, milliseconds: int) -> None:
        import time

        time.sleep(milliseconds / 1000)

    def start(self, storage_state: Path | None = None) -> None:
        """Démarre le moteur, éventuellement avec une session déjà authentifiée."""

    def save_storage_state(self, path: Path) -> None:
        """Persiste les cookies/session pour éviter de se reconnecter à chaque run."""

    def screenshot(self, path: Path) -> None:
        """Optionnel : capture l'état de la page (utile en cas d'échec)."""

    def close(self) -> None:
        """Libère les ressources. Doit être idempotent."""
