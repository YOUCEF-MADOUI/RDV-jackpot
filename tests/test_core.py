"""Tests du noyau : aucun navigateur, aucune dépendance externe.

Lancement :  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from signup_bot.accounts import AccountError, load_account
from signup_bot.adapters.mock_adapter import MockAdapter
from signup_bot.audit import AuditLog, mask_email, mask_phone
from signup_bot.guardrails import RateLimitError, RateLimiter
from signup_bot.models import ProfileError, load_profile
from signup_bot.conditions import Condition, ConditionError
from signup_bot.notify import Notifier
from signup_bot.recipes import RecipeError, load_recipe, render
from signup_bot.runner import Runner

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "example.profile.json"
RECIPE = ROOT / "recipes" / "demo_local.json"
LOOP_RECIPE = ROOT / "recipes" / "demo_loop.json"
ACCOUNT = ROOT / "accounts" / "example.account.json"


class RenderTests(unittest.TestCase):
    def test_remplace_les_variables(self) -> None:
        self.assertEqual(render("Bonjour {{ first_name }}", {"first_name": "Alice"}), "Bonjour Alice")

    def test_refuse_une_variable_inconnue(self) -> None:
        with self.assertRaises(RecipeError):
            render("{{ nope }}", {"first_name": "Alice"})

    def test_variable_avec_espaces(self) -> None:
        self.assertEqual(render("{{  city  }}", {"city": "Sétif"}), "Sétif")


class ProfileTests(unittest.TestCase):
    def test_exemple_valide(self) -> None:
        profile = load_profile(PROFILE)
        self.assertEqual(profile.first_name, "Alice")
        self.assertEqual(profile.address.country, "DZ")
        self.assertNotIn("password", profile.as_mapping())

    def test_email_invalide(self) -> None:
        profile = load_profile(PROFILE)
        with self.assertRaises(ProfileError):
            replace(profile, email="pas-un-email").validate()

    def test_telephone_non_e164(self) -> None:
        profile = load_profile(PROFILE)
        with self.assertRaises(ProfileError):
            replace(profile, phone="0555 12 34 56").validate()

    def test_consentement_obligatoire(self) -> None:
        profile = load_profile(PROFILE)
        with self.assertRaises(ProfileError):
            replace(profile, consents={"terms": True}).validate()

    def test_photo_falsifiee_refusee(self) -> None:
        profile = load_profile(PROFILE)
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "photo.png"
            fake.write_text("<script>alert(1)</script>", encoding="utf-8")
            with self.assertRaises(ProfileError):
                replace(profile, photo_path=fake).validate()

    def test_mot_de_passe_lu_dans_lenvironnement(self) -> None:
        profile = load_profile(PROFILE)
        os.environ["TEST_SIGNUP_PASSWORD"] = "s3cret"
        try:
            self.assertEqual(replace(profile, password_env="TEST_SIGNUP_PASSWORD").password(), "s3cret")
        finally:
            os.environ.pop("TEST_SIGNUP_PASSWORD", None)

    def test_mot_de_passe_absente_echoue(self) -> None:
        profile = load_profile(PROFILE)
        with self.assertRaises(ProfileError):
            replace(profile, password_env="VARIABLE_QUI_N_EXISTE_PAS").password()

    def test_date_naissance_future_refusee(self) -> None:
        profile = load_profile(PROFILE)
        with self.assertRaises(ProfileError):
            replace(profile, birthdate="2999-01-01").validate()


class RecipeTests(unittest.TestCase):
    def test_recette_demo_valide(self) -> None:
        recipe = load_recipe(RECIPE)
        self.assertGreater(len(recipe.steps), 5)
        self.assertFalse(recipe.requires_human_pause())

    def test_recette_non_autorisee_refusee(self) -> None:
        raw = json.loads(RECIPE.read_text(encoding="utf-8"))
        raw["authorized"] = False
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(RecipeError):
                load_recipe(path)

    def test_hote_hors_liste_refuse(self) -> None:
        recipe = load_recipe(RECIPE)
        with self.assertRaises(RecipeError):
            recipe.assert_url_allowed("https://exemple-malveillant.tld/inscription")

    def test_sous_domaine_wildcard_autorise(self) -> None:
        recipe = load_recipe(RECIPE)
        with self.assertRaises(RecipeError):
            recipe.assert_url_allowed("http://evil.127.0.0.1/")

    def test_action_inconnue_refusee(self) -> None:
        raw = json.loads(RECIPE.read_text(encoding="utf-8"))
        raw["steps"].append({"action": "resolve_captcha", "selector": "#x"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(RecipeError):
                load_recipe(path)

    def test_secret_env_reserve_a_fill(self) -> None:
        raw = json.loads(RECIPE.read_text(encoding="utf-8"))
        raw["steps"].append({"action": "click", "selector": "#x", "secret_env": "MDP"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(RecipeError):
                load_recipe(path)


class RunnerTests(unittest.TestCase):
    def _run(self, tmp: Path, **kwargs):
        adapter = MockAdapter(**kwargs.pop("adapter_kwargs", {}))
        recipe = load_recipe(RECIPE)
        profile = load_profile(PROFILE)
        os.environ["DEMO_PASSWORD"] = "mot-de-passe-de-test"
        try:
            runner = Runner(
                adapter=adapter,
                recipe=recipe,
                profile=profile,
                audit=AuditLog(tmp / "audit.jsonl"),
                artifacts_dir=tmp / "artifacts",
                **kwargs,
            )
            return runner.run(), adapter
        finally:
            os.environ.pop("DEMO_PASSWORD", None)

    def test_parcours_complet_en_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, adapter = self._run(Path(tmp), dry_run=True)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.completed_steps, len(load_recipe(RECIPE).steps))
            self.assertEqual(adapter.visited, [load_recipe(RECIPE).start_url])
            self.assertTrue(adapter.closed)

    def test_dry_run_sans_secret(self) -> None:
        """Une simulation ne doit exiger aucun mot de passe réel."""
        with tempfile.TemporaryDirectory() as tmp:
            adapter = MockAdapter()
            result = Runner(
                adapter=adapter,
                recipe=load_recipe(RECIPE),
                profile=load_profile(PROFILE),
                audit=AuditLog(Path(tmp) / "audit.jsonl"),
                artifacts_dir=Path(tmp) / "artifacts",
                dry_run=True,
            ).run()
            self.assertTrue(result.ok, result.error)
            password_calls = [c for c in adapter.calls if c.get("selector") == "#password"]
            self.assertEqual(password_calls[0]["value"], "***")

    def test_le_mot_de_passe_napparait_pas_dans_le_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._run(Path(tmp))
            self.assertTrue(result.ok, result.error)
            log = (Path(tmp) / "audit.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("mot-de-passe-de-test", log)
            self.assertIn("***", log)
            # les données personnelles sont masquées
            self.assertNotIn("alice.benali@example.com", log)

    def test_echec_du_navigateur_est_journalise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._run(Path(tmp), adapter_kwargs={"fail_on": "fill"})
            self.assertFalse(result.ok)
            self.assertIn("échec simulé", result.error)
            self.assertTrue(result.completed_steps < len(load_recipe(RECIPE).steps))

    def test_limitation_de_debit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._run(Path(tmp))
            result, _ = self._run(Path(tmp), run_id="deuxieme-essai")
            self.assertFalse(result.ok)
            self.assertIn("réessayez dans", result.error)

    def test_recette_contenant_pause_humaine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = json.loads(RECIPE.read_text(encoding="utf-8"))
            raw["steps"].insert(0, {"action": "pause_for_human", "prompt": "CAPTCHA"})
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            adapter = MockAdapter()
            os.environ["DEMO_PASSWORD"] = "mot-de-passe-de-test"
            result = Runner(
                adapter=adapter,
                recipe=load_recipe(path),
                profile=load_profile(PROFILE),
                audit=AuditLog(Path(tmp) / "audit.jsonl"),
                artifacts_dir=Path(tmp) / "artifacts",
            ).run()
            os.environ.pop("DEMO_PASSWORD", None)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(adapter.human_pauses, ["CAPTCHA"])


class GuardrailTests(unittest.TestCase):
    def test_intervalle_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            limiter = RateLimiter(Path(tmp) / "state.json", min_interval_seconds=60)
            limiter.check("a")
            limiter.mark("a")
            with self.assertRaises(RateLimitError):
                limiter.check("a")
            limiter.check("b")  # une autre clé n'est pas bloquée

    def test_fichier_debit_corrompu_ne_plante_pas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("pas du json", encoding="utf-8")
            RateLimiter(path, 60).check("a")


class MaskTests(unittest.TestCase):
    def test_masquage(self) -> None:
        self.assertEqual(mask_email("alice@example.com"), "a***@example.com")
        self.assertEqual(mask_phone("+213555123456"), "***456")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Boucle « connexion → actions → si la notification X, on recommence »
# ---------------------------------------------------------------------------

ESSAI = {"texts": ["Aucun créneau disponible pour le moment."], "selectors": []}
SUCCES = {"texts": ["Réservation confirmée !"], "selectors": []}
ESPACE = {"texts": [], "selectors": ["#reserver"]}
FORMULAIRE = {"texts": [], "selectors": ["#username", "#password"]}
RIEN = {"texts": [], "selectors": []}


class FakeClock:
    """Horloge virtuelle : aucune attente réelle dans les tests."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class LoopTests(unittest.TestCase):
    def _run(self, tmp: Path, scenario, *, recipe_path=LOOP_RECIPE, **kwargs):
        clock = FakeClock()
        adapter = MockAdapter(scenario=scenario)
        os.environ["DEMO_PASSWORD"] = "mot-de-passe-de-test"
        try:
            runner = Runner(
                adapter=adapter,
                recipe=load_recipe(recipe_path),
                account=load_account(ACCOUNT),
                audit=AuditLog(tmp / "audit.jsonl"),
                artifacts_dir=tmp / "artifacts",
                sleep=clock.sleep,
                clock=clock,
                run_id=kwargs.pop("run_id", "run-1"),
                **kwargs,
            )
            result = runner.run()
        finally:
            os.environ.pop("DEMO_PASSWORD", None)
        return result, adapter, runner, clock

    def test_reussite_apres_deux_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scenario = [RIEN, FORMULAIRE, ESPACE, ESPACE, ESPACE, ESSAI, ESPACE, SUCCES]
            result, adapter, runner, clock = self._run(Path(tmp), scenario)
            self.assertTrue(result.ok, result.error or result.reason)
            self.assertEqual(result.attempts, 2)
            self.assertIn("créneau obtenu", result.matched)
            self.assertIn("notification : aucun créneau", result.matched)
            # 1 clic de connexion + 1 clic par essai
            self.assertEqual(adapter.count("click"), 3)
            # une seule attente entre les deux essais, >= intervalle de la recette
            self.assertEqual(len(clock.slept), 1)
            self.assertGreaterEqual(clock.slept[0], 5)

    def test_mot_de_passe_jamais_dans_le_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scenario = [RIEN, FORMULAIRE, ESPACE, ESPACE, ESPACE, ESSAI, ESPACE, SUCCES]
            result, _, _, _ = self._run(Path(tmp), scenario)
            self.assertTrue(result.ok, result.error or result.reason)
            log = (Path(tmp) / "audit.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("mot-de-passe-de-test", log)
            self.assertNotIn('"alice"', log)  # l'identifiant est masqué aussi
            self.assertIn('"***"', log)

    def test_arret_sur_condition_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bloque = {"texts": ["Compte suspendu"], "selectors": []}
            result, adapter, _, _ = self._run(Path(tmp), [RIEN, FORMULAIRE, ESPACE, ESPACE, ESPACE, bloque])
            self.assertEqual(result.status, "stopped")
            self.assertEqual(result.attempts, 1)
            self.assertEqual(adapter.count("click"), 2)  # connexion + 1 essai

    def test_abandon_si_page_inattendue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inattendu = {"texts": ["Erreur 500"], "selectors": []}
            result, _, _, _ = self._run(
                Path(tmp), [RIEN, FORMULAIRE, ESPACE, ESPACE, ESPACE, inattendu]
            )
            self.assertEqual(result.status, "abandoned")
            self.assertIn("inattendu", result.reason)

    def test_plafond_dessais_et_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = json.loads(LOOP_RECIPE.read_text(encoding="utf-8"))
            raw["loop"]["max_attempts"] = 3
            raw["loop"]["max_interval_seconds"] = 100
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            # l'espace puis une série d'échecs (le mock répète le dernier état)
            result, _, _, clock = self._run(
                Path(tmp), [RIEN, FORMULAIRE, ESPACE, ESPACE, ESSAI], recipe_path=path
            )
            self.assertEqual(result.status, "exhausted")
            self.assertEqual(result.attempts, 3)
            self.assertEqual(len(clock.slept), 2)
            # back-off : la deuxième pause est plus longue que la première
            self.assertGreater(clock.slept[1], clock.slept[0])

    def test_duree_maximale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = json.loads(LOOP_RECIPE.read_text(encoding="utf-8"))
            raw["loop"].update(
                {"max_attempts": 50, "interval_seconds": 100, "max_interval_seconds": 100,
                 "max_duration_seconds": 250, "jitter_seconds": 0}
            )
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            result, _, _, clock = self._run(
                Path(tmp), [RIEN, FORMULAIRE, ESPACE, ESPACE, ESSAI], recipe_path=path
            )
            self.assertEqual(result.status, "exhausted")
            self.assertIn("durée maximale", result.reason)
            self.assertLessEqual(clock.now, 250 + 100)

    def test_session_reutilisee_pas_de_reconnexion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # premier run : connexion complète puis succès immédiat
            result, adapter1, _, _ = self._run(
                Path(tmp), [RIEN, FORMULAIRE, ESPACE, ESPACE, SUCCES]
            )
            self.assertTrue(result.ok, result.error or result.reason)
            self.assertTrue(adapter1.saved_sessions)

            # second run : la page est déjà connectée → aucun remplissage de mot de passe
            session = Path(adapter1.saved_sessions[0])
            self.assertTrue(session.is_file())
            result2, adapter2, _, _ = self._run(
                Path(tmp),
                [ESPACE, SUCCES],
                run_id="run-2",
                enforce_rate_limit=False,
            )
            self.assertTrue(result2.ok, result2.error or result2.reason)
            self.assertEqual(adapter2.loaded_session, str(session))
            mot_de_passe = [c for c in adapter2.calls if c.get("selector") == "#password"]
            self.assertEqual(mot_de_passe, [])

    def test_connexion_refusee(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            refus = {"texts": ["Identifiant ou mot de passe incorrect."], "selectors": []}
            result, _, _, _ = self._run(Path(tmp), [RIEN, FORMULAIRE, refus])
            self.assertEqual(result.status, "failed")
            self.assertIn("connexion échouée", result.error)

    def test_secret_manquant_avant_ouverture_navigateur(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = MockAdapter(scenario=[RIEN, FORMULAIRE, ESPACE])
            clock = FakeClock()
            result = Runner(
                adapter=adapter,
                recipe=load_recipe(LOOP_RECIPE),
                account=load_account(ACCOUNT),
                audit=AuditLog(Path(tmp) / "audit.jsonl"),
                artifacts_dir=Path(tmp) / "artifacts",
                sleep=clock.sleep,
                clock=clock,
            ).run()
            self.assertEqual(result.status, "failed")
            self.assertIn("DEMO_PASSWORD", result.error)
            # rien n'a été ouvert ni rempli : le navigateur n'a pas été lancé
            actions = {call["action"] for call in adapter.calls}
            self.assertNotIn("goto", actions)
            self.assertNotIn("fill", actions)
            self.assertNotIn("start", actions)

    def test_notification_envoyee(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "notifications.jsonl"
            scenario = [RIEN, FORMULAIRE, ESPACE, ESPACE, SUCCES]
            result, _, _, _ = self._run(
                Path(tmp), scenario, notifier=Notifier(outbox, events=("success",))
            )
            self.assertTrue(result.ok, result.error or result.reason)
            lines = [json.loads(line) for line in outbox.read_text().splitlines()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["event"], "success")
            self.assertEqual(lines[0]["site"], "demo-local")


class LoginRecipeTests(unittest.TestCase):
    def test_login_et_loop_charges(self) -> None:
        recipe = load_recipe(LOOP_RECIPE)
        self.assertIsNotNone(recipe.login)
        self.assertIsNotNone(recipe.loop)
        self.assertEqual(recipe.all_secret_envs(), set())  # secrets via compte
        self.assertTrue(recipe.login.reuse_session)

    def test_loop_sans_success_when_refusee(self) -> None:
        raw = json.loads(LOOP_RECIPE.read_text(encoding="utf-8"))
        del raw["loop"]["success_when"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(RecipeError):
                load_recipe(path)

    def test_plancher_de_politesse_sur_site_distant(self) -> None:
        raw = json.loads(LOOP_RECIPE.read_text(encoding="utf-8"))
        raw["allowed_hosts"] = ["portail.example.com"]
        raw["start_url"] = "https://portail.example.com/espace"
        raw["loop"]["reset_steps"] = [{"action": "goto", "value": "https://portail.example.com/espace"}]
        raw["loop"]["interval_seconds"] = 2
        raw["login"]["url"] = "https://portail.example.com/connexion"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            recipe = load_recipe(path)
            assert recipe.loop is not None
            self.assertGreaterEqual(recipe.effective_loop().interval_seconds, 15)

    def test_hote_de_la_boucle_verifie(self) -> None:
        raw = json.loads(LOOP_RECIPE.read_text(encoding="utf-8"))
        raw["loop"]["reset_steps"] = [{"action": "goto", "value": "https://ailleurs.example/"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(RecipeError):
                load_recipe(path)

    def test_compte_validations(self) -> None:
        account = load_account(ACCOUNT)
        self.assertEqual(account.username, "alice")
        self.assertEqual(account.password_env, "DEMO_PASSWORD")
        with self.assertRaises(AccountError):
            replace(account, username="").validate()
        with self.assertRaises(AccountError):
            replace(account, password_env="pas un nom de variable").validate()
        with self.assertRaises(AccountError):
            replace(account, password_env="ABSENTE_DU_SHELL").password()

    def test_conditions(self) -> None:
        adapter = MockAdapter(page_texts=["Aucun créneau disponible"], visible_selectors=["#x"])
        self.assertTrue(Condition("text", "aucun créneau").matches(adapter))
        self.assertFalse(Condition("text", "Réservation confirmée").matches(adapter))
        self.assertTrue(Condition("selector", "#x").matches(adapter))
        adapter.open("https://exemple.test/espace")
        self.assertTrue(Condition("url_contains", "/espace").matches(adapter))
        with self.assertRaises(ConditionError):
            from signup_bot.conditions import condition_from

            condition_from({"kind": "inconnu", "value": "x"}, "test")


class DemoCoherenceTests(unittest.TestCase):
    """La recette de démo doit rester cohérente avec le HTML du site de démo.

    C'est le test « anti-dérive » : si quelqu'un renomme un champ du formulaire,
    la recette ne doit pas rester silencieusement cassée.
    """

    def setUp(self) -> None:
        self.server_source = (ROOT / "demo" / "server.py").read_text(encoding="utf-8")
        self.recipe = load_recipe(LOOP_RECIPE)

    def _steps(self) -> list:
        steps = list(self.recipe.steps)
        assert self.recipe.login is not None and self.recipe.loop is not None
        steps += list(self.recipe.login.steps)
        steps += list(self.recipe.loop.steps)
        steps += list(self.recipe.loop.reset_steps)
        return steps

    def test_les_selecteurs_existent_dans_le_html(self) -> None:
        for step in self._steps():
            selector = step.selector
            if selector.startswith("#"):
                self.assertIn(f'id="{selector[1:]}"', self.server_source, selector)
            elif selector.startswith("button[type=submit]"):
                self.assertIn('type="submit"', self.server_source)

    def test_les_textes_de_condition_existent(self) -> None:
        # retry/success sont réels sur le site de démo ; stop_when est illustratif
        assert self.recipe.loop is not None
        self.assertTrue(self.recipe.loop.stop_when)  # une boucle doit toujours avoir un arrêt
        for condition in list(self.recipe.loop.retry_when) + list(self.recipe.loop.success_when):
            if condition.kind == "text":
                self.assertIn(condition.value, self.server_source, condition.value)

    def test_les_urls_pointent_vers_le_site_de_demo(self) -> None:
        assert self.recipe.login is not None
        self.assertTrue(self.recipe.is_local())
        self.assertIn("/connexion", self.recipe.login.url)
