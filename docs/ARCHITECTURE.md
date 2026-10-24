# Architecture et guide d'implémentation

Ce document complète le README : il détaille le modèle de données, le cycle
d'exécution, la façon d'ajouter un site, et les points d'extension.

## 0. Mode boucle (connexion → actions → notification → recommencer)

```
run()
 ├─ préflight : autorisation, hôtes, secrets présents, débit autorisé
 ├─ adaptateur.start(session)      ← session réutilisée si valide
 ├─ login  : logged_in_when ? → sinon steps → success_when (attente bornée)
 ├─ open(start_url)
 └─ boucle :
      pour chaque essai (≤ max_attempts, ≤ max_duration_seconds) :
        1. (essai 1 seulement) pré-contrôle indulgent : succès/arrêt déjà là ?
        2. reset_steps  → état connu
        3. steps        → boutons prédéfinis
        4. observe : success_when → SUCCÈS
                     stop_when    → ARRÊT
                     retry_when   → pause (intervalle + jitter, back-off) → essai suivant
                     sinon        → ABANDON (pas de clic au hasard)
      épuisement → EXHAUSTED (notification envoyée)
```

Ordre de priorité **succès > arrêt > réessai > abandon** : il doit être explicite
dans le code, sinon une page contenant à la fois « Réservation confirmée » et
« Aucun créneau disponible » (historique, bandeau) provoque des comportements
incohérents. Les conditions sont des **observations** (jamais des actions) et ne
lèvent pas d'exception quand l'élément est absent.

Débit : `effective_loop()` remonte l'intervalle au plancher de politesse
(15 s sur un site distant, 1 s sur `127.0.0.1`/`localhost`) et applique un plafond
de 500 essais. Le jitter évite une cadence parfaitement mécanique.

Durées d'attente et horloge sont injectables (`sleep=`, `clock=`) : les tests
utilisent une horloge virtuelle, donc aucun test n'attend réellement.

## 1. Modèle de données

### Profil (`profiles/*.profile.json`)

| Champ | Exemple | Règle |
|---|---|---|
| `profile_id` | `demo-alice` | identifiant stable, utilisé dans les journaux |
| `first_name`, `last_name` | `Alice`, `Benali` | non vides |
| `email` | `alice.benali@example.com` | format e-mail |
| `phone` | `+213555123456` | **E.164 obligatoire** |
| `birthdate` | `1994-06-12` | `AAAA-MM-JJ`, dans le passé |
| `address` | `line1`, `line2`, `postal_code`, `city`, `country` | `country` = ISO-2 |
| `photo` | `photos/alice.png` | chemin relatif au profil, < 5 Mo, contenu vérifié |
| `password_env` | `DEMO_PASSWORD` | **nom** de variable d'environnement, pas le mot de passe |
| `consents` | `terms`, `data_processing` | les deux doivent être `true` |

Le mapping exposé aux recettes (`Profile.as_mapping`) : `first_name`, `last_name`,
`full_name`, `email`, `phone`, `phone_local` (sans `+`), `birthdate`,
`address_line1`, `address_line2`, `postal_code`, `city`, `country`, `photo_path`,
`locale`.

### Recette (`recipes/*.json`)

```jsonc
{
  "site": "mon-site",
  "start_url": "https://mon-site.example/inscription",
  "allowed_hosts": ["mon-site.example", "*.mon-site.example"],
  "authorized": true,                       // obligatoire, sinon refus
  "authorization_note": "site interne, autorisation du 12/06/2026",
  "success_text": "Votre compte est créé",
  "min_interval_seconds": 60,
  "steps": [ /* voir actions */ ]
}
```

### Actions disponibles

| Action | Champs | Effet |
|---|---|---|
| `goto` | `value` (URL) | navigue, **hôte vérifié** contre `allowed_hosts` |
| `fill` | `selector`, `value` *ou* `secret_env` | remplit un champ (ou un secret non journalisé) |
| `select` | `selector`, `value` | liste déroulante |
| `check` | `selector` | case à cocher / radio |
| `upload` | `selector`, `value` (`{{ photo_path }}`) | envoie un fichier |
| `click` | `selector` | clic |
| `press` | `selector`, `value` (touche) | clavier (`Enter`, `Tab`…) |
| `wait_for` | `selector` | attend l'apparition d'un élément |
| `assert_text` | `contains` | vérifie un texte visible (validation finale) |
| `pause_for_human` | `prompt` | **arrêt** : un humain agit, puis Entrée |
| `wait_ms` | `value` | temporisation explicite (à éviter si possible) |

Toute valeur peut contenir `{{ variable }}` issue du profil. Une variable inconnue
fait **échouer la validation** (pas de champ vide silencieux).

## 2. Cycle d'exécution

```
validate profil ──► validate recette ──► rate limit ──► dossier run unique
        │                                                    │
        ▼                                                    ▼
   erreurs claires  ◄── échec ◄── étape N ◄── navigateur ◄── journalisation
                                                        │
                                        succès ──► assert success_text
```

Chaque étape produit un événement `step` dans `artifacts/audit.jsonl`
(valeurs PII masquées, secrets en `***`). En cas d'échec : `run_failed` +
`artifacts/<run_id>/failure.png`.

## 3. Ajouter un site en 6 étapes

1. **Obtenir l'autorisation** (CGU, contrat, ou site qui vous appartient).
2. Ouvrir la page d'inscription, inspecter les champs : préférer `#id`, `[name=…]`,
   sinon `label` (« Prénom »), et seulement en dernier recours un sélecteur structurel.
3. Créer `recipes/<site>.json` en partant de `recipes/demo_local.json`.
4. Renseigner `allowed_hosts` avec **uniquement** les domaines du parcours
   (page de confirmation, redirection OAuth éventuelle…).
5. `python -m signup_bot check --recipe recipes/<site>.json` pour valider.
6. `--dry-run`, puis un essai `--headful` sur un compte de test, enfin seulement
   un profil réel.

## 4. Points d'extension

| Point | Comment |
|---|---|
| Nouveau moteur (API, Appium) | implémenter `adapters.base.Adapter` et le passer au `Runner` |
| Nouvelle action | ajouter le nom dans `SUPPORTED_ACTIONS` + la validation + un `elif` dans `Runner._execute` + un test |
| Interface web | appeler `Runner` depuis FastAPI, un job = un `run_id` |
| Notifications | brancher un `AuditLog` « composite » ou un hook après `run_succeeded` |
| Double authentification / OTP | `pause_for_human` en mode `--headful`, ou API officielle avec jeton |

## 5. Checklist sécurité / conformité

- [ ] La photo et le mot de passe ne sont jamais journalisés.
- [ ] Les profils réels ne sont pas dans Git (`.gitignore` couvre `*.profile.json`).
- [ ] `authorized: true` n'est mis qu'après vérification réelle de l'autorisation.
- [ ] `allowed_hosts` est minimal (pas de `*` global).
- [ ] `min_interval_seconds` ≥ 60 pour tout site qui n'est pas le vôtre.
- [ ] Un humain traite les CAPTCHA/OTP — aucun contournement automatisé.
- [ ] Durée de conservation des profils et des captures définie, purge planifiée.
- [ ] Un moyen de supprimer un profil et ses artefacts (droit à l'effacement).

## 6. Stratégie de tests

| Niveau | Outil | Ce qui est vérifié |
|---|---|---|
| Unitaire | `unittest` | validation profil, rendu des variables, garde-fous, masquage |
| Intégration sans navigateur | `MockAdapter` | ordre des étapes, secrets, échecs, limitation de débit |
| Bout en bout | Playwright + `demo/server.py` | formulaire réel rempli, photo reçue, page de succès |
| Non-régression par site | hash de structure de page | alerte quand un site change son formulaire |

En CI : seuls les deux premiers niveaux (rapides, sans réseau) ; le bout en bout
tourne sur un runner dédié avec le navigateur installé.
