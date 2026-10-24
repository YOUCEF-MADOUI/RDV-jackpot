"""Adaptateur Playwright : un vrai navigateur, piloté pas à pas.

Installation :
    pip install -r requirements.txt
    playwright install chromium

Choix assumé : navigateur standard, aucune option « stealth ». Si un site bloque
l'automatisation, la réponse correcte est une autorisation/API officielle ou une
action humaine — pas un contournement technique.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..errors import AutomationError
from .base import Adapter, AdapterError

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class PlaywrightAdapter(Adapter):
    def __init__(
        self,
        *,
        headless: bool = True,
        browser: str = "chromium",
        locale: str = "fr-FR",
        user_agent: str = "",
        slow_mo_ms: int = 0,
        channel: str = "",
    ):
        self.headless = headless
        self.browser_name = browser
        self.locale = locale
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.slow_mo_ms = slow_mo_ms
        self.channel = channel
        self._playwright = None
        self._browser = None
        self._context = None
        self._storage_state: Path | None = None
        self.page = None

    # ------------------------------------------------------------- cycle de vie
    def start(self, storage_state: Path | None = None) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - dépend de l'environnement
            raise AutomationError(
                "Playwright n'est pas installé. Lancez :\n"
                "  pip install -r requirements.txt\n"
                "  playwright install chromium"
            ) from exc

        if storage_state is not None and not Path(storage_state).is_file():
            storage_state = None
        self._storage_state = Path(storage_state) if storage_state else None

        self._playwright = sync_playwright().start()
        launcher = getattr(self._playwright, self.browser_name, None)
        if launcher is None:
            self._playwright.stop()
            self._playwright = None
            raise AutomationError(f"navigateur Playwright inconnu : {self.browser_name!r}")
        kwargs = {"headless": self.headless, "slow_mo": self.slow_mo_ms}
        if self.channel:
            kwargs["channel"] = self.channel
        try:
            self._browser = launcher.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001 - message d'aide à l'utilisateur
            self._playwright.stop()
            self._playwright = None
            raise AutomationError(
                f"impossible de démarrer {self.browser_name} : {exc}\n"
                f"Installez le navigateur avec : playwright install {self.browser_name}"
            ) from exc

        context_kwargs = {
            "locale": self.locale,
            "user_agent": self.user_agent,
            "viewport": {"width": 1366, "height": 900},
            "accept_downloads": False,
        }
        if self._storage_state is not None:
            context_kwargs["storage_state"] = str(self._storage_state)
        self._context = self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(15_000)
        self.page = self._context.new_page()

    def save_storage_state(self, path: Path) -> None:
        """Persiste cookies + localStorage : la prochaine fois, pas de reconnexion."""
        if self._context is None:
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._context.storage_state(path=str(path))
        except Exception:  # noqa: BLE001 - non bloquant
            pass

    def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    closer.close()
                except Exception:  # noqa: BLE001 - fermeture best effort
                    pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        self.page = None

    # ---------------------------------------------------------------- actions
    def _page(self):
        if self.page is None:
            raise AdapterError("navigateur non démarré (appelez start() d'abord)")
        return self.page

    def open(self, url: str) -> None:
        self._page().goto(url, wait_until="domcontentloaded")

    def current_url(self) -> str:
        return self.page.url if self.page is not None else ""

    def navigate_back(self) -> None:
        self._page().go_back(wait_until="domcontentloaded")

    def fill(self, selector: str, value: str, timeout_ms: int) -> None:
        self._page().fill(selector, value, timeout=timeout_ms)

    def select(self, selector: str, value: str, timeout_ms: int) -> None:
        self._page().select_option(selector, value, timeout=timeout_ms)

    def check(self, selector: str, timeout_ms: int) -> None:
        self._page().check(selector, timeout=timeout_ms)

    def upload(self, selector: str, path: Path, timeout_ms: int) -> None:
        if not Path(path).is_file():
            raise AdapterError(f"fichier à envoyer introuvable : {path}")
        self._page().set_input_files(selector, str(path), timeout=timeout_ms)

    def click(self, selector: str, timeout_ms: int) -> None:
        self._page().click(selector, timeout=timeout_ms)

    def press(self, selector: str, key: str, timeout_ms: int) -> None:
        self._page().press(selector, key, timeout=timeout_ms)

    def wait_for(self, selector: str, timeout_ms: int) -> None:
        self._page().wait_for_selector(selector, timeout=timeout_ms)

    def assert_text(self, text: str, timeout_ms: int) -> None:
        self._page().get_by_text(text, exact=False).first.wait_for(timeout=timeout_ms)

    def text_present(self, text: str, timeout_ms: int) -> bool:
        try:
            self._page().get_by_text(text, exact=False).first.wait_for(
                timeout=timeout_ms, state="visible"
            )
            return True
        except Exception:  # noqa: BLE001 - absence = réponse négative, pas une erreur
            return False

    def selector_present(self, selector: str, timeout_ms: int) -> bool:
        try:
            self._page().wait_for_selector(selector, timeout=timeout_ms, state="visible")
            return True
        except Exception:  # noqa: BLE001
            return False

    def pause_for_human(self, prompt: str) -> None:
        if self.headless:
            raise AdapterError(
                "pause_for_human exige un navigateur visible : relancez avec --headful"
            )
        print(f"\n[ACTION HUMAINE] {prompt}")
        input("Appuyez sur Entrée une fois l'étape terminée… ")

    def screenshot(self, path: Path) -> None:
        if self.page is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.page.screenshot(path=str(path), full_page=True)
        except Exception:  # noqa: BLE001 - la capture ne doit jamais masquer l'erreur
            pass
