"""Assistant d'inscription automatisée — noyau réutilisable.

Point d'entrée : `python -m signup_bot --help`.
"""

from __future__ import annotations

from .models import Address, Profile, ProfileError, load_profile
from .recipes import Recipe, RecipeError, Step, load_recipe, render
from .runner import Runner, RunResult

__all__ = [
    "Address",
    "Profile",
    "ProfileError",
    "Recipe",
    "RecipeError",
    "RunResult",
    "Runner",
    "Step",
    "load_profile",
    "load_recipe",
    "render",
]

__version__ = "0.1.0"
