"""Garde-fous : limitation de débit, horodatage des exécutions, artefacts.

L'objectif n'est pas de « passer inaperçu » (ce serait du contournement d'anti-bot),
mais de rester un utilisateur raisonnable : une inscription à la fois, espacée,
traçable, et arrêtable.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


class RateLimitError(RuntimeError):
    """Tentative d'exécution trop rapprochée de la précédente."""


@dataclass
class RateLimiter:
    """Empêche de relancer le même couple (profil, site) trop souvent."""

    state_path: Path
    min_interval_seconds: int

    def remaining(self, key: str) -> float:
        data = self._read()
        last = data.get(key)
        if last is None:
            return 0.0
        elapsed = time.time() - float(last)
        return max(0.0, self.min_interval_seconds - elapsed)

    def check(self, key: str) -> None:
        left = self.remaining(key)
        if left > 0:
            raise RateLimitError(
                f"exécution récente pour {key!r} : réessayez dans {left:.0f} s "
                f"(intervalle minimum : {self.min_interval_seconds} s)."
            )

    def mark(self, key: str) -> None:
        data = self._read()
        data[key] = time.time()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)  # écriture atomique

    def _read(self) -> dict[str, float]:
        if not self.state_path.is_file():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in dict(data).items()}
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}


def prepare_artifacts_dir(path: Path, run_id: str, *, overwrite: bool = False) -> Path:
    target = path / run_id
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"{target} existe déjà : une exécution identique a peut-être eu lieu. "
            "Relancez avec un autre identifiant de run."
        )
    target.mkdir(parents=True, exist_ok=True)
    return target
