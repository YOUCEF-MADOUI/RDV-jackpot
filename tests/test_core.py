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

from signup_bot.adapters.mock_adapter import MockAdapter
from signup_bot.audit import AuditLog, mask_email, mask_phone
from signup_bot.guardrails import RateLimitError, RateLimiter
from signup_bot.models import ProfileError, load_profile
from signup_bot.recipes import RecipeError, load_recipe, render
from signup_bot.runner import Runner

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "example.profile.json"
RECIPE = ROOT / "recipes" / "demo_local.json"


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
