"""Exceptions utilisateur du projet (messages clairs, pas de stack trace brute)."""

from __future__ import annotations


class AutomationError(RuntimeError):
    """Erreur d'automatisation : navigateur indisponible, page inattendue, etc."""


__all__ = ["AutomationError"]
