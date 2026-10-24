"""Comptes : identifiants de connexion fournis par le PROPRIÉTAIRE du compte.

Un compte = « qui se connecte sur le site ». Un profil = « pour qui / quelles
données ». Les deux sont séparés : un même profil peut être utilisé avec plusieurs
comptes, et un compte peut ne pas avoir besoin de données personnelles du tout.

Le mot de passe n'est JAMAIS dans le fichier : on n'y met que le NOM de la
variable d'environnement qui le contient. C'est ce qui permet de versionner le
fichier sans fuite, et de tourner en production avec un coffre-fort de secrets.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class AccountError(ValueError):
    """Compte invalide : message destiné à l'utilisateur."""


@dataclass(frozen=True)
class Account:
    account_id: str
    site: str
    username: str
    password_env: str = "SIGNUP_PASSWORD"
    profile_path: Path | None = None
    enabled: bool = True
    note: str = ""
    source_path: Path | None = None

    def validate(self, *, need_password: bool = False) -> None:
        if not ACCOUNT_ID_RE.match(self.account_id):
            raise AccountError(
                "account_id doit contenir uniquement lettres, chiffres, '.', '_' ou '-'"
            )
        if not self.site.strip():
            raise AccountError("site est obligatoire")
        if not self.username.strip():
            raise AccountError("username est obligatoire")
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", self.password_env):
            raise AccountError(
                "password_env doit être un nom de variable d'environnement valide"
            )
        if self.profile_path is not None and not self.profile_path.is_file():
            raise AccountError(f"profil associé introuvable : {self.profile_path}")
        if need_password:
            self.password()

    def password(self) -> str:
        value = os.environ.get(self.password_env, "")
        if not value:
            raise AccountError(
                f"mot de passe absent : la variable d'environnement {self.password_env!r} "
                f"est vide (compte {self.account_id!r})."
            )
        return value

    def as_mapping(self) -> dict[str, str]:
        """Variables utilisables dans les étapes de connexion (jamais le mot de passe)."""
        return {
            "account_id": self.account_id,
            "username": self.username,
            "site": self.site,
        }


def load_account(path: str | Path, *, need_password: bool = False) -> Account:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise AccountError(f"fichier de compte introuvable : {path}")
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AccountError(f"JSON invalide dans {path.name} : {exc}") from exc
    if not isinstance(raw, Mapping):
        raise AccountError("un fichier de compte doit être un objet JSON")

    def required(key: str) -> str:
        if key not in raw:
            raise AccountError(f"champ obligatoire manquant : {key!r}")
        return str(raw[key])

    profile_raw = raw.get("profile")
    profile_path = Path(str(profile_raw)) if profile_raw else None
    if profile_path is not None and not profile_path.is_absolute():
        profile_path = (path.parent / profile_path).resolve()

    account = Account(
        account_id=str(raw.get("account_id") or path.stem),
        site=required("site"),
        username=required("username"),
        password_env=str(raw.get("password_env", "SIGNUP_PASSWORD")),
        profile_path=profile_path,
        enabled=bool(raw.get("enabled", True)),
        note=str(raw.get("note", "")),
        source_path=path,
    )
    account.validate(need_password=need_password)
    return account
