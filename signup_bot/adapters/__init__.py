"""Adaptateurs d'exécution (navigateur réel ou simulé)."""

from __future__ import annotations

from .base import Adapter, AdapterError
from .mock_adapter import MockAdapter

__all__ = ["Adapter", "AdapterError", "MockAdapter", "PlaywrightAdapter"]


def __getattr__(name: str):
    """Import paresseux : Playwright n'est requis que si on l'utilise vraiment."""
    if name == "PlaywrightAdapter":
        from .playwright_adapter import PlaywrightAdapter

        return PlaywrightAdapter
    raise AttributeError(name)
