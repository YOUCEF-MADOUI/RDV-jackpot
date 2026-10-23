# RDV-jackpot — assistant d'inscription automatisée

> **Ce dépôt est un squelette fonctionnel + le guide d'architecture** pour un logiciel
> qui remplit automatiquement un formulaire d'inscription (nom, prénom, adresse,
> téléphone, photo de profil…) à partir d'informations saisies à l'avance.
>
> Le code fourni fonctionne de bout en bout **sur le site de démonstration local**
> inclus (`demo/server.py`). Il est volontairement conçu pour n'agir que sur des sites
> dont vous êtes propriétaire ou pour lesquels vous avez une autorisation écrite
> (`"authorized": true` dans chaque recette), et **n'intègre aucun contournement**
> de CAPTCHA ou de détection de robot. Voir [la section légale](#5-limites-légales-et-éthiques-à-lire-avant-tout).

---

## Sommaire

1. [Ce que fait le projet](#1-ce-que-fait-le-projet)
2. [Démarrage en 5 minutes](#2-démarrage-en-5-minutes)
3. [Architecture recommandée](#3-architecture-recommandée)
4. [Les choix techniques, expliqués](#4-les-choix-techniques-expliqués)
5. [Limites légales et éthiques (à lire avant tout)](#5-limites-légales-et-éthiques-à-lire-avant-tout)
6. [Passer à l'échelle (production)](#6-passer-à-léchelle-production)
7. [Feuille de route](#7-feuille-de-route)

---

## 1. Ce que fait le projet

```
profil (JSON)  +  recette (JSON)  ──►  moteur (runner)  ──►  navigateur  ──►  site
   données           parcours             orchestration       Playwright      (le vôtre)
```

* **Un profil** = une personne : `profiles/example.profile.json`
  (identité, adresse, téléphone, photo, consentements). Le mot de passe n'y est
  **jamais** : on ne stocke que le nom de la variable d'environnement qui le contient.
* **Une recette** = le mode d'emploi d'un site : `recipes/demo_local.json`
  (liste d'étapes `goto`, `fill`, `upload`, `click`, `assert_text`…).
  Ajouter un site = ajouter un fichier JSON, **pas** un nouveau programme.
* **Un runner** = exécute la recette avec le profil, journalise chaque étape
  (audit JSONL), capture l'écran en cas d'échec, applique un débit maximal.

Commandes disponibles :

| Commande | Rôle |
|---|---|
| `python -m signup_bot validate --profile profiles/example.profile.json` | vérifie les données et la photo |
| `python -m signup_bot check --recipe recipes/demo_local.json` | vérifie la recette, l'autorisation et les domaines |
| `python -m signup_bot run … --dry-run` | simule tout le parcours **sans** navigateur |
| `python -m signup_bot run …` | exécution réelle (Chromium, `--headful` pour voir) |
| `python -m signup_bot doctor` | vérifie que Playwright et le navigateur sont installés |

---

## 2. Démarrage en 5 minutes

```bash
# 1. dépendances (le cœur ne demande que la bibliothèque standard)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # télécharge le navigateur (~150 Mo)

# 2. vérifier l'environnement, le profil et la recette
python -m signup_bot doctor
python -m signup_bot validate --profile profiles/example.profile.json
python -m signup_bot check    --recipe   recipes/demo_local.json

# 3. lancer le site de démonstration (autre terminal)
python -m demo.server                # http://127.0.0.1:8000/inscription

# 4. simulation, puis exécution réelle
export DEMO_PASSWORD='MotDePasseDeTest123!'
python -m signup_bot run --profile profiles/example.profile.json \
       --recipe recipes/demo_local.json --dry-run
python -m signup_bot run --profile profiles/example.profile.json \
       --recipe recipes/demo_local.json --headful
```

Résultat attendu : le formulaire est rempli, la photo envoyée, la page
« Inscription confirmée » détectée, et `demo/submissions/` contient l'enregistrement.

Tests du noyau (aucune dépendance, aucun réseau) :

```bash
python -m unittest discover -s tests -v      # 26 tests
```

---

## 3. Architecture recommandée

### 3.1 Vue d'ensemble

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 1. SAISIE / STOCKAGE                       (phase 1 : fichiers JSON)     │
│    profils (PII)  ──────────────►  coffre-fort de secrets (mot de passe)  │
│    photos (JPEG/PNG, <5 Mo)                                              │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 2. MÉTIER                                    (signup_bot/models.py)      │
│    validation : e-mail, E.164, date, image réelle (magic bytes), consentements
└──────────────────────────────────┬───────────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 3. PARCOURS                                  (recipes/*.json → recipes.py)
│    goto → fill → upload → check → click → assert_text → pause_for_human  │
│    garde-fous : hôtes autorisés, étapes typées, secrets par variable d'env│
└──────────────────────────────────┬───────────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 4. ORCHESTRATION                             (runner.py, guardrails.py)  │
│    débit max 1 essai / intervalle, idempotence, journal d'audit, capture │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 5. EXÉCUTION                      (adapters/ : Playwright | Mock | API)  │
│    contrat commun → on peut remplacer le navigateur sans rien réécrire   │
└──────────────────────────────────────────────────────────────────────────┘
```

**Le point clé de l'architecture : la séparation profil / recette / moteur.**
Les données d'une personne ne doivent jamais être mélangées au code du site, et le
parcours d'un site ne doit jamais être codé en dur dans le programme. C'est ce qui
permet de :

* supporter 1 site ou 50 sites avec le même moteur ;
* tester un parcours **sans navigateur** (adaptateur `MockAdapter`) ;
* changer d'outil d'automatisation (Playwright → Selenium → Appium → API officielle)
  sans réécrire la logique métier ;
* auditer ce qui a été envoyé, à qui, et quand.

### 3.2 Pourquoi un parcours déclaratif (recettes JSON) plutôt que du code ?

| Approche | Avantages | Inconvénients |
|---|---|---|
| Script Python par site | rapide à démarrer | duplication, pas testable, secrets dans le code |
| **Recettes déclaratives (choisi)** | un fichier = un site, validable, versionnable, relu par un non-développeur | limité aux actions connues (extensible) |
| LLM qui « regarde » la page et clique | très souple sur sites inconnus | lent, coûteux, imprévisible, risque d'envoyer de mauvaises données (PII !) |
| API officielle du site | le plus fiable et le plus rapide | n'existe pas toujours ; à privilégier quand elle existe |

> Règle pratique : **API officielle > recette déclarative > LLM en simple assistant
> de création de recette**. Un LLM peut proposer les sélecteurs CSS d'une page, mais
> il ne doit pas être celui qui clique en production avec des données personnelles.

### 3.3 Idempotence et reprise sur erreur

* un identifiant de `run` unique par exécution ; un dossier d'artefacts existant
  bloque une seconde exécution identique ;
* l'intervalle minimal entre deux essais pour un couple (profil, site) est appliqué
  par `guardrails.RateLimiter` (état dans `artifacts/.state/`) ;
* en cas d'échec : capture d'écran + dernière étape atteinte dans le journal ;
* la vérification finale (`success_text` / `assert_text`) évite de déclarer
  « succès » un formulaire qui a en réalité affiché une erreur de validation.

---

## 4. Les choix techniques, expliqués

| Besoin | Choix retenu | Pourquoi |
|---|---|---|
| Piloter un navigateur | **Playwright (Python, sync)** | auto-attente des sélecteurs, multi-navigateurs, `set_input_files` fiable pour la photo, bien plus stable que Selenium sur les formulaires modernes |
| Parcours | **JSON déclaratif** | lisible, versionnable, validé avant exécution, zéro code par site |
| Validation des données | **bibliothèque standard** | téléphone E.164, e-mail, date réelle, type d'image par *magic bytes* (pas l'extension) |
| Secrets | **variables d'environnement** | le mot de passe ne doit jamais être dans un JSON, un log ou Git |
| Journal | **JSONL append-only, PII masquées** | exploitable par `grep`/`jq`, probant en cas de litige |
| Tests | **`unittest` + `MockAdapter`** | CI sans navigateur, exécution en quelques millisecondes |
| Interface (phase 3) | **FastAPI + Jinja/HTMX ou React** | API claire, file d'attente, dashboard d'exécutions |

**Détails qui font la différence en pratique :**

* **Photo** : accepter JPEG/PNG/WebP, vérifier la taille (< 5 Mo) *et* le contenu réel ;
  beaucoup de sites refusent silencieusement une image trop lourde ou mal nommée.
* **Téléphone** : toujours normaliser en **E.164** (`+213555123456`) et garder une
  variante locale (`phone_local`) pour les champs qui refusent le `+`.
* **Champs dynamiques** : préférer `label=`/`get_by_label` ou `name=` aux sélecteurs
  CSS fragile du type `.sc-bdVaJa > div:nth-child(3)`.
* **Codes OTP / CAPTCHA** : ce sont des **points de passage humains**. Le parcours
  s'arrête (`pause_for_human`), l'utilisateur agit, puis on continue. Voir §5.
* **Zéro « mode furtif »** : pas de rotation de proxy, pas de faux fingerprint.
  Si le site bloque, c'est un signal : demandez une autorisation ou utilisez son API.

---

## 5. Limites légales et éthiques (à lire avant tout)

Ce projet crée des comptes **au nom d'une personne**. Trois règles à ne pas franchir :

1. **Consentement de la personne concernée.** Chaque profil contient
   `consents.terms` et `consents.data_processing`. Remplir un formulaire au nom de
   quelqu'un sans son accord peut relever de l'usurpation d'identité et viole le
   RGPD (art. 5-6) / la loi 18-07 en Algérie sur la protection des données.
2. **Autorisation du site.** Automatiser un service tiers est presque toujours
   interdit par ses CGU. Les recettes exigent `"authorized": true` et ne peuvent
   visiter que les domaines listés dans `allowed_hosts`. Les usages légitimes :
   votre propre site, un site client, un extranet partenaire, un concours de
   démonstration.
3. **Pas de contournement technique.** Ne jamais résoudre un CAPTCHA
   automatiquement, ni simuler une empreinte de navigateur pour tromper un
   anti-bot : dans plusieurs juridictions (dont la France, art. 323-1 du Code
   pénal) c'est une atteinte à un système de traitement automatisé de données.
   Pour les tests de charge de votre propre site, utilisez plutôt des outils dédiés
   (k6, Locust) sur un environnement de staging.

**Ce que le dépôt ne fera pas**, volontairement : remplir un CAPTCHA, créer des
comptes en masse (le débit est plafonné par conception), ni réutiliser les données
d'une personne sans son accord. Un `CAPTCHA` ou un `OTP` déclenche une pause
humaine, ou une demande d'accès à l'API officielle.

Pour les données personnelles :

* profils réels hors du dépôt (`.gitignore`), chiffrés au repos en production ;
* durée de conservation définie (par ex. supprimer le profil après l'inscription) ;
* droit d'accès/suppression : un simple `rm` du profil + purge des artefacts ;
* ne jamais journaliser mot de passe, OTP, ni le contenu de la photo.

---

## 6. Passer à l'échelle (production)

Le squelette est mono-processus. Pour un vrai produit multi-utilisateurs :

```
┌───────────┐   ┌──────────────┐   ┌──────────────┐   ┌─────────────────┐
│ Frontend  │──►│ API (FastAPI)│──►│ File (Redis/ │──►│ Workers         │
│ (dashboard│   │ auth, CRUD,  │   │  RabbitMQ)   │   │ Playwright      │
│  profils, │   │ quotas       │   │              │   │ (1 conteneur    │
│  recettes)│   └──────┬───────┘   └──────────────┘   │  = 1 navigateur)│
└───────────┘          │                              └────────┬────────┘
                       ▼                                       ▼
              ┌─────────────────┐                    ┌────────────────────┐
              │ PostgreSQL      │                    │ Stockage objets    │
              │ profils chiffrés│                    │ photos + captures  │
              │ + audit         │                    │ (S3/MinIO, TTL)    │
              └─────────────────┘                    └────────────────────┘
```

* **Workers isolés** : un conteneur par exécution, navigateur en `headless`,
  limites CPU/mémoire, `timeout` dur (par ex. 120 s) ;
* **File d'attente** : retries avec back-off, priorité, annulation depuis le
  dashboard, `job_id` = `run_id` ;
* **Secrets** : Vault/AWS Secrets Manager, jamais dans la file ni dans les logs ;
* **Observabilité** : métriques Prometheus (taux de succès par site, durée par étape),
  traces OpenTelemetry, alerte si un site change de structure (le taux d'échec
  sur `wait_for` monte) ;
* **Détection de changement de formulaire** : enregistrer un « hash de structure »
  de la page et notifier quand il change (c'est la panne n°1 de ce type de logiciel) ;
* **Chiffrement** : colonnes PII chiffrées (Fernet/KMS), audit immuable ;
* **Conformité** : registre de traitement, base légale, DPO si volume important.

Ordre de mise en œuvre conseillé : CLI (fait) → dashboard profils → file d'attente
→ multi-sites → notifications (e-mail/Telegram quand une inscription est terminée
ou attend une action humaine).

---

## 7. Feuille de route

| Phase | Contenu | Statut |
|---|---|---|
| 1 | Profils validés, recettes déclaratives, runner + audit, site de démo, tests | ✅ ce dépôt |
| 2 | Assistant de création de recette (ouvre la page, propose les sélecteurs, enregistre le JSON) | à faire |
| 3 | API FastAPI + dashboard (profils, recettes, exécutions, reprise) | à faire |
| 4 | File d'attente + workers conteneurisés + métriques | à faire |
| 5 | Connecteur API officiel (prioritaire sur le navigateur quand disponible) | à faire |

---

## Structure du dépôt

```
signup_bot/            moteur (aucune dépendance obligatoire)
├── models.py          profil + validation (E.164, photo, consentements)
├── recipes.py         recettes JSON + rendu {{ variables }}
├── runner.py          orchestration, journal, reprise
├── guardrails.py      débit max, état des exécutions
├── audit.py           journal JSONL, masquage des PII
├── cli.py             validate / check / run / doctor
└── adapters/          base.py (contrat), playwright_adapter.py, mock_adapter.py
recipes/               parcours par site (demo_local.json)
profiles/              profils (exemple fictif uniquement) + photos/
demo/                  site de démonstration local
tests/                 26 tests, sans réseau
docs/ARCHITECTURE.md   détails d'architecture et d'exploitation
```

Licence : à définir par le propriétaire du dépôt.
