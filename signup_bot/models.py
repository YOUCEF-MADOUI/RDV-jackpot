"""Modèle de données d'un profil d'inscription + validation.

Principe : le profil décrit UNE personne réelle, qui consent à l'inscription.
Les données sont séparées en deux :
  - les données "métier" (identité, adresse, photo) stockées dans le profil ;
  - les secrets (mot de passe, code PIN) qui ne sont JAMAIS dans le fichier :
    on ne stocke que le NOM de la variable d'environnement (password_env).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping

PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")  # format E.164, ex: +213555123456
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_PHOTO_BYTES = 5 * 1024 * 1024  # 5 Mo, taille vue couramment sur les formulaires


class ProfileError(ValueError):
    """Profil invalide : message destiné à l'utilisateur, pas une stack trace."""


def _sniff_image(path: Path) -> str | None:
    """Détecte le type réel du fichier par ses octets d'en-tête (pas l'extension)."""
    with path.open("rb") as handle:
        head = handle.read(16)
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass(frozen=True)
class Address:
    line1: str
    postal_code: str
    city: str
    country: str  # code ISO 3166-1 alpha-2 (ex: "DZ", "FR")
    line2: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "address_line1": self.line1,
            "address_line2": self.line2,
            "postal_code": self.postal_code,
            "city": self.city,
            "country": self.country,
        }


@dataclass(frozen=True)
class Profile:
    profile_id: str
    first_name: str
    last_name: str
    email: str
    phone: str
    birthdate: str
    address: Address
    photo_path: Path
    password_env: str
    locale: str = "fr"
    consents: Mapping[str, bool] = field(default_factory=dict)
    source_path: Path | None = None

    # ------------------------------------------------------------------ rendu
    def as_mapping(self) -> dict[str, str]:
        """Variables utilisables dans les recettes : {{ first_name }}, etc."""
        data = {
            "profile_id": self.profile_id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": f"{self.first_name} {self.last_name}",
            "email": self.email,
            "phone": self.phone,
            "phone_local": self.phone.lstrip("+"),
            "birthdate": self.birthdate,
            "photo_path": str(self.photo_path),
            "locale": self.locale,
        }
        data.update(self.address.as_dict())
        return data

    def password(self) -> str:
        """Le mot de passe n'est lu qu'au moment de l'exécution."""
        value = os.environ.get(self.password_env, "")
        if not value:
            raise ProfileError(
                f"Variable d'environnement {self.password_env!r} absente ou vide. "
                "Exportez-la avant de lancer une exécution réelle."
            )
        return value

    # -------------------------------------------------------------- validation
    def validate(self, *, need_password: bool = False) -> None:
        checks = {
            "first_name": (self.first_name, "le prénom est obligatoire"),
            "last_name": (self.last_name, "le nom est obligatoire"),
            "email": (self.email, "l'e-mail est obligatoire"),
            "phone": (self.phone, "le téléphone est obligatoire"),
            "birthdate": (self.birthdate, "la date de naissance est obligatoire"),
        }
        for value, message in checks.values():
            if not value or not str(value).strip():
                raise ProfileError(message)

        if not EMAIL_RE.match(self.email):
            raise ProfileError(f"e-mail invalide : {self.email!r}")
        if not PHONE_RE.match(self.phone):
            raise ProfileError(
                f"téléphone invalide : {self.phone!r} — format attendu E.164, ex. +213555123456"
            )
        if not ISO_DATE_RE.match(self.birthdate):
            raise ProfileError("birthdate doit être au format AAAA-MM-JJ")
        try:
            born = date.fromisoformat(self.birthdate)
        except ValueError as exc:
            raise ProfileError(f"date de naissance impossible : {self.birthdate!r}") from exc
        if born >= date.today():
            raise ProfileError("la date de naissance doit être dans le passé")

        if not self.address.line1.strip() or not self.address.city.strip():
            raise ProfileError("adresse incomplète : line1 et city sont obligatoires")
        if not re.match(r"^[A-Z]{2}$", self.address.country):
            raise ProfileError("country doit être un code ISO à 2 lettres, ex. DZ")

        if not self.photo_path.is_file():
            raise ProfileError(f"photo introuvable : {self.photo_path}")
        size = self.photo_path.stat().st_size
        if size > MAX_PHOTO_BYTES:
            raise ProfileError(
                f"photo trop lourde ({size / 1_048_576:.1f} Mo) : maximum 5 Mo"
            )
        if _sniff_image(self.photo_path) is None:
            raise ProfileError("la photo doit être un JPEG, PNG ou WebP valide")

        missing = [name for name, ok in self.consents.items() if not ok]
        if missing or not self.consents:
            raise ProfileError(
                "consentements manquants : le profil doit attester l'accord de la personne "
                "(consentements requis : 'terms' et 'data_processing')"
            )
        for required in ("terms", "data_processing"):
            if not self.consents.get(required):
                raise ProfileError(f"consentement {required!r} non donné")

        if need_password:
            self.password()  # lève si le secret n'est pas disponible


def _require(raw: Mapping[str, Any], key: str) -> Any:
    if key not in raw:
        raise ProfileError(f"champ obligatoire manquant dans le profil : {key!r}")
    return raw[key]


def load_profile(path: str | Path, *, need_password: bool = False) -> Profile:
    """Charge et valide un profil JSON. Les chemins relatifs partent du fichier."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ProfileError(f"profil introuvable : {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"JSON invalide dans {path.name} : {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError("le profil doit être un objet JSON")

    address_raw = _require(raw, "address")
    try:
        address = Address(
            line1=str(address_raw["line1"]),
            line2=str(address_raw.get("line2", "")),
            postal_code=str(address_raw["postal_code"]),
            city=str(address_raw["city"]),
            country=str(address_raw["country"]).upper(),
        )
    except (KeyError, TypeError) as exc:
        raise ProfileError(
            "address doit contenir line1, postal_code, city et country"
        ) from exc

    photo_raw = str(_require(raw, "photo"))
    photo_path = Path(photo_raw)
    if not photo_path.is_absolute():
        photo_path = (path.parent / photo_path).resolve()

    profile = Profile(
        profile_id=str(raw.get("profile_id") or path.stem),
        first_name=str(_require(raw, "first_name")).strip(),
        last_name=str(_require(raw, "last_name")).strip(),
        email=str(_require(raw, "email")).strip(),
        phone=str(_require(raw, "phone")).strip(),
        birthdate=str(_require(raw, "birthdate")).strip(),
        address=address,
        photo_path=photo_path,
        password_env=str(raw.get("password_env", "SIGNUP_PASSWORD")),
        locale=str(raw.get("locale", "fr")),
        consents=dict(raw.get("consents") or {}),
        source_path=path,
    )
    profile.validate(need_password=need_password)
    return profile
