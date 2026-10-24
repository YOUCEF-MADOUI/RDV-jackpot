"""CLI : `python -m signup_bot <commande>`.

Sous-commandes :
  validate  vérifier un profil (JSON propre, photo correcte, consentements)
  account   vérifier un fichier de compte (identifiant + variable de mot de passe)
  check     vérifier une recette (actions, sélecteurs, domaines, boucle)
  run       exécuter : connexion → actions → boucle jusqu'au résultat
  doctor    vérifier l'environnement (Playwright installé ? navigateur présent ?)
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .accounts import AccountError, load_account
from .adapters import MockAdapter
from .audit import AuditLog
from .errors import AutomationError
from .models import ProfileError, load_profile
from .notify import Notifier
from .recipes import RecipeError, load_recipe
from .runner import Runner

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NOT_REACHED = 3  # exécution correcte mais objectif non atteint (arrêt, abandon…)


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


def _print_account(account) -> None:
    print(f"Compte      : {account.account_id}")
    print(f"Site        : {account.site}")
    print(f"Identifiant : {account.username}")
    print(f"Mot de passe: variable d'environnement {account.password_env}")
    print(f"Actif       : {'oui' if account.enabled else 'non'}")
    if account.profile_path:
        print(f"Profil lié  : {account.profile_path}")
    if account.note:
        print(f"Note        : {account.note}")


def _print_recipe(recipe) -> None:
    print(f"Site         : {recipe.site}")
    print(f"URL de départ: {recipe.start_url}")
    print(f"Domaines     : {', '.join(recipe.allowed_hosts)}")
    print(f"Intervalle   : {recipe.min_interval_seconds} s minimum entre deux runs")
    if recipe.login is not None:
        print(f"Connexion    : {recipe.login.url} "
              f"({len(recipe.login.steps)} étape(s), session réutilisée : "
              f"{'oui' if recipe.login.reuse_session else 'non'})")
        if recipe.login.success_when:
            print("  preuve de connexion : "
                  + ", ".join(c.label() for c in recipe.login.success_when))
    if recipe.steps:
        print(f"Actions      : {len(recipe.steps)} étape(s)")
        for index, step in enumerate(recipe.steps, start=1):
            print(f"  {index:>2}. {step.describe()}")
    loop = recipe.effective_loop()
    if loop is not None:
        print("Boucle       : oui")
        print(f"  réessai si : {', '.join(c.label() for c in loop.retry_when)}")
        print(f"  succès si  : {', '.join(c.label() for c in loop.success_when)}")
        if loop.stop_when:
            print(f"  arrêt si   : {', '.join(c.label() for c in loop.stop_when)}")
        print(f"  max        : {loop.max_attempts} essais / {loop.max_duration_seconds} s")
        print(f"  cadence    : {loop.interval_seconds:g} s → "
              f"{loop.max_interval_seconds:g} s (back-off ×{loop.backoff_factor:g}, "
              f"jitter ≤ {loop.jitter_seconds:g} s)")
        print(f"  actions    : {len(loop.steps)} étape(s) par essai")
        for index, step in enumerate(loop.steps, start=1):
            print(f"    {index:>2}. {step.describe()}")
    if recipe.notify:
        print(f"Notifications: {', '.join(str(n) for n in recipe.notify)}")


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        print(f"[INVALIDE] {exc}", file=sys.stderr)
        return EXIT_FAILED
    _print_profile(profile)
    print("\n[OK] profil valide.")
    return EXIT_OK


def cmd_account(args: argparse.Namespace) -> int:
    try:
        account = load_account(args.account, need_password=args.with_password)
    except AccountError as exc:
        print(f"[INVALIDE] {exc}", file=sys.stderr)
        return EXIT_FAILED
    _print_account(account)
    print("\n[OK] compte valide (aucun mot de passe écrit dans le fichier).")
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
        print(f"Chromium    : indisponible ({type(exc).__name__})")
        print("              lancez : playwright install --with-deps chromium")
        return EXIT_FAILED
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    try:
        recipe = load_recipe(args.recipe)
    except RecipeError as exc:
        print(f"[INVALIDE] {exc}", file=sys.stderr)
        return EXIT_USAGE

    profile = account = None
    try:
        if args.profile:
            profile = load_profile(args.profile)
        if args.account:
            account = load_account(args.account)
    except (ProfileError, AccountError) as exc:
        print(f"[INVALIDE] {exc}", file=sys.stderr)
        return EXIT_USAGE

    if recipe.login is not None and account is None and not args.dry_run:
        print(
            "[ERREUR] cette recette se connecte à un compte : fournissez --account "
            "(identifiant + variable d'environnement du mot de passe).",
            file=sys.stderr,
        )
        return EXIT_USAGE

    artifacts = Path(args.artifacts).expanduser().resolve()
    audit = AuditLog(Path(args.audit).expanduser().resolve())
    notifier = Notifier(
        outbox=Path(args.outbox).expanduser().resolve(),
        events=tuple(str(e) for e in recipe.notify) or ("success", "stopped",
                                                       "abandoned", "exhausted", "failed"),
        webhook_env=args.webhook_env,
    )

    if args.dry_run:
        print("[DRY-RUN] aucun navigateur ne sera ouvert, aucun formulaire ne sera envoyé.")
        print(f"Site    : {recipe.site} — {recipe.start_url}")
        if account is not None:
            print(f"Compte  : {account.account_id} ({account.username})")
        if profile is not None:
            print(f"Profil  : {profile.profile_id} ({profile.first_name} "
                  f"{profile.last_name})")
        _print_recipe(recipe)
        # Simulation : tout élément/texte est considéré présent, aucune attente réelle.
        adapter = MockAdapter(assume_present=True)
        recipe = _for_simulation(recipe)
    else:
        from .adapters.playwright_adapter import PlaywrightAdapter

        adapter = PlaywrightAdapter(
            headless=not args.headful,
            browser=args.browser,
            locale=recipe.locale,
            user_agent=recipe.user_agent,
            slow_mo_ms=args.slow_mo,
        )
        if recipe.requires_human_pause() and not args.headful:
            print(
                "[ATTENTION] la recette contient une étape humaine (CAPTCHA/OTP) : "
                "elle échouera en mode headless. Utilisez --headful.",
                file=sys.stderr,
            )

    clock = _VirtualClock() if args.dry_run else None
    runner = Runner(
        sleep=clock.sleep if clock else _noop,
        clock=clock if clock else __import__("time").monotonic,
        adapter=adapter,
        recipe=recipe,
        profile=profile,
        account=account,
        audit=audit,
        artifacts_dir=artifacts,
        dry_run=args.dry_run,
        enforce_rate_limit=not args.no_rate_limit,
        run_id=args.run_id,
        notifier=notifier,
    )
    result = runner.run()

    print(f"\nRun {result.run_id} : {result.status.upper()} — {result.reason or ''}")
    print(f"Essais {result.attempts} | étapes {result.completed_steps} | "
          f"pauses cumulées {runner.slept_seconds:.1f} s")
    if args.dry_run:
        print("[SIMULATION] parcours rejoué hors ligne : les conditions sont supposées "
              "vraies pour vérifier la mécanique.")
    if result.matched:
        print(f"Signaux : {', '.join(result.matched)}")
    print(f"Artefacts : {result.artifacts_dir}")
    print(f"Journal   : {audit.path}")
    if notifier.sent:
        print(f"Notifications : {notifier.outbox} ({len(notifier.sent)})")
    if result.error:
        print(f"Erreur    : {result.error}", file=sys.stderr)
    if result.ok:
        return EXIT_OK
    if result.status in ("stopped", "abandoned", "exhausted"):
        return EXIT_NOT_REACHED
    return EXIT_FAILED


def _for_simulation(recipe):
    """En simulation, on veut VOIR la mécanique : on rejoue la connexion et un essai.

    Sans ces deux ajustements, tout étant « présent », le pré-contrôle trouverait
    immédiatement le succès et sauterait les étapes — le dry-run ne testerait rien.
    """
    if recipe.login is not None and recipe.login.logged_in_when:
        recipe = replace(
            recipe, login=replace(recipe.login, logged_in_when=(), reuse_session=False)
        )
    if recipe.loop is not None and recipe.loop.evaluate_before_steps:
        recipe = replace(recipe, loop=replace(recipe.loop, evaluate_before_steps=False))
    return recipe


def _noop(_seconds: float) -> None:
    """Aucune attente en mode simulation."""


class _VirtualClock:
    """Horloge accélérée du dry-run : le temps passe sans attendre."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signup_bot",
        description="Assistant d'automatisation de parcours web (connexion, actions, boucle).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="valider un profil")
    p_validate.add_argument("--profile", required=True)
    p_validate.set_defaults(func=cmd_validate)

    p_account = sub.add_parser("account", help="valider un fichier de compte")
    p_account.add_argument("--account", required=True)
    p_account.add_argument("--with-password", action="store_true",
                           help="vérifier aussi que la variable de mot de passe existe")
    p_account.set_defaults(func=cmd_account)

    p_check = sub.add_parser("check", help="valider une recette")
    p_check.add_argument("--recipe", required=True)
    p_check.set_defaults(func=cmd_check)

    p_run = sub.add_parser("run", help="exécuter un parcours")
    p_run.add_argument("--recipe", required=True)
    p_run.add_argument("--account", default=None, help="fichier de compte (connexion)")
    p_run.add_argument("--profile", default=None, help="fichier de profil (données)")
    p_run.add_argument("--dry-run", action="store_true", help="simuler sans navigateur")
    p_run.add_argument("--headful", action="store_true", help="afficher le navigateur")
    p_run.add_argument("--browser", default="chromium", choices=["chromium", "firefox", "webkit"])
    p_run.add_argument("--slow-mo", type=int, default=0, help="ralentir les actions (ms)")
    p_run.add_argument("--artifacts", default="artifacts")
    p_run.add_argument("--audit", default="artifacts/audit.jsonl")
    p_run.add_argument("--outbox", default="artifacts/notifications.jsonl")
    p_run.add_argument("--webhook-env", default="SIGNUP_WEBHOOK_URL",
                       help="nom de la variable contenant l'URL de webhook (optionnel)")
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
