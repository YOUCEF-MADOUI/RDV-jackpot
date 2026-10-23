"""CLI : `python -m signup_bot <commande>`.

Sous-commandes :
  validate  vérifier un profil (JSON propre, photo correcte, consentements)
  check     vérifier une recette (actions, sélecteurs, domaines autorisés)
  run       exécuter une inscription (dry-run par défaut ? non : dry-run explicite)
  doctor    vérifier l'environnement (Playwright installé ? navigateur présent ?)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import MockAdapter
from .audit import AuditLog
from .errors import AutomationError
from .models import ProfileError, load_profile
from .recipes import RecipeError, load_recipe
from .runner import Runner

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _print_profile(profile) -> None:
    print(f"Profil      : {profile.profile_id}")
    print(f"Identité    : {profile.first_name} {profile.last_name}")
    print(f"E-mail      : {profile.email}")
    print(f"Téléphone   : {profile.phone}")
    print(f"Naissance   : {profile.birthdate}")
    print(f"Adresse     : {profile.address.line1}, {profile.address.postal_code} "
          f"{profile.address.city}, {profile.address.country}")
    print(f"Photo       : {profile.photo_path} "
          f"({profile.photo_path.stat().st_size // 1024} Ko)")
    print(f"Mot de passe: variable d'environnement {profile.password_env}")


def _print_recipe(recipe) -> None:
    print(f"Site         : {recipe.site}")
    print(f"URL de départ: {recipe.start_url}")
    print(f"Domaines     : {', '.join(recipe.allowed_hosts)}")
    print(f"Intervalle   : {recipe.min_interval_seconds} s minimum entre deux essais")
    print(f"Étapes       : {len(recipe.steps)}")
    for index, step in enumerate(recipe.steps, start=1):
        print(f"  {index:>2}. {step.describe()}")


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        profile = load_profile(args.profile, need_password=False)
    except ProfileError as exc:
        print(f"[INVALIDE] {exc}", file=sys.stderr)
        return EXIT_FAILED
    _print_profile(profile)
    print("\n[OK] profil valide.")
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    try:
        recipe = load_recipe(args.recipe)
    except RecipeError as exc:
        print(f"[INVALIDE] {exc}", file=sys.stderr)
        return EXIT_FAILED
    _print_recipe(recipe)
    print("\n[OK] recette autorisée et valide.")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"Python      : {sys.version.split()[0]}")
    try:
        import playwright  # noqa: F401

        print("Playwright  : installé (paquet Python)")
    except ImportError:
        print("Playwright  : ABSENT — pip install -r requirements.txt")
        print("              puis : playwright install chromium")
        return EXIT_FAILED
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        print("Chromium    : disponible")
    except Exception as exc:  # noqa: BLE001
        print(f"Chromium    : indisponible ({exc})")
        print("              lancez : playwright install --with-deps chromium")
        return EXIT_FAILED
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    try:
        recipe = load_recipe(args.recipe)
        profile = load_profile(args.profile, need_password=not args.dry_run)
    except (RecipeError, ProfileError) as exc:
        print(f"[INVALIDE] {exc}", file=sys.stderr)
        return EXIT_USAGE

    artifacts = Path(args.artifacts).expanduser().resolve()
    audit = AuditLog(Path(args.audit).expanduser().resolve())
    run_id = args.run_id

    if args.dry_run:
        print("[DRY-RUN] aucun navigateur ne sera ouvert, aucun formulaire ne sera envoyé.")
        print(f"Site   : {recipe.site} — {recipe.start_url}")
        print(f"Profil : {profile.profile_id} ({profile.first_name} {profile.last_name})")
        print("Étapes :")
        for index, step in enumerate(recipe.steps, start=1):
            print(f"  {index:>2}. {step.describe()}")
        adapter = MockAdapter()
    else:
        from .adapters.playwright_adapter import PlaywrightAdapter

        adapter = PlaywrightAdapter(
            headless=not args.headful,
            browser=args.browser,
            locale=recipe.locale or profile.locale,
            user_agent=recipe.user_agent,
            slow_mo_ms=args.slow_mo,
        )
        if recipe.requires_human_pause() and not args.headful:
            print(
                "[ATTENTION] la recette contient une étape humaine (CAPTCHA/OTP) : "
                "elle échouera en mode headless. Utilisez --headful.",
                file=sys.stderr,
            )

    runner = Runner(
        adapter=adapter,
        recipe=recipe,
        profile=profile,
        audit=audit,
        artifacts_dir=artifacts,
        dry_run=args.dry_run,
        enforce_rate_limit=not args.no_rate_limit,
        run_id=run_id,
    )
    result = runner.run()

    print(f"\nRun {result.run_id} : {result.status.upper()} "
          f"({result.completed_steps}/{len(recipe.steps)} étapes, "
          f"{result.duration_seconds:.1f} s)")
    print(f"Artefacts : {result.artifacts_dir}")
    print(f"Journal   : {audit.path}")
    if not result.ok:
        print(f"Erreur    : {result.error}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signup_bot",
        description="Assistant d'inscription automatisée (profils + recettes de sites).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="valider un profil")
    p_validate.add_argument("--profile", required=True)
    p_validate.set_defaults(func=cmd_validate)

    p_check = sub.add_parser("check", help="valider une recette de site")
    p_check.add_argument("--recipe", required=True)
    p_check.set_defaults(func=cmd_check)

    p_run = sub.add_parser("run", help="exécuter une inscription")
    p_run.add_argument("--profile", required=True)
    p_run.add_argument("--recipe", required=True)
    p_run.add_argument("--dry-run", action="store_true", help="simuler sans navigateur")
    p_run.add_argument("--headful", action="store_true", help="afficher le navigateur")
    p_run.add_argument("--browser", default="chromium", choices=["chromium", "firefox", "webkit"])
    p_run.add_argument("--slow-mo", type=int, default=0, help="ralentir les actions (ms)")
    p_run.add_argument("--artifacts", default="artifacts")
    p_run.add_argument("--audit", default="artifacts/audit.jsonl")
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--no-rate-limit", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_doctor = sub.add_parser("doctor", help="vérifier l'installation")
    p_doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrompu.", file=sys.stderr)
        return 130
    except AutomationError as exc:
        print(f"[ERREUR] {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
