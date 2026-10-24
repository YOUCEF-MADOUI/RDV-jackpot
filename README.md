# RDV-jackpot — assistant d'automatisation de parcours web

> **Squelette fonctionnel + guide d'architecture** pour un logiciel qui, à partir
> d'identifiants fournis par le propriétaire du compte :
> **se connecte → clique sur des boutons prédéfinis → lit une notification → si elle
> apparaît, recommence → jusqu'au résultat voulu.**
> Le même moteur sait aussi remplir un formulaire d'inscription (nom, prénom,
> adresse, téléphone, photo).
>
> Tout est testé de bout en bout sur le **site de démonstration local** inclus
> (`demo/server.py`). Le code n'agit que sur des sites dont vous êtes propriétaire
> ou pour lesquels une autorisation écrite existe (`"authorized": true`), et
> **n'intègre aucun contournement** de CAPTCHA ou de détection de robot.
> Voir [§6 Limites légales](#6-limites-légales-et-éthiques-à-lire-avant-tout).

---

## Sommaire

1. [Ce que fait le projet](#1-ce-que-fait-le-projet)
2. [Le mode boucle, expliqué](#2-le-mode-boucle-expliqué-le-cœur-du-besoin)
3. [Démarrage en 5 minutes](#3-démarrage-en-5-minutes)
4. [Architecture recommandée](#4-architecture-recommandée)
5. [Les choix techniques, expliqués](#5-les-choix-techniques-expliqués)
6. [Limites légales et éthiques (à lire avant tout)](#6-limites-légales-et-éthiques-à-lire-avant-tout)
7. [Passer à l'échelle (production)](#7-passer-à-léchelle-production)
8. [Feuille de route](#8-feuille-de-route)

---

## 1. Ce que fait le projet

```
compte (JSON)  +  profil (JSON)  +  recette (JSON)  ──►  moteur  ──►  navigateur  ──►  site
identifiants       données PII        parcours           runner      Playwright     (le vôtre)
   │                                     │
   └ mot de passe : UNIQUEMENT via variable d'environnement ┘
```

Trois fichiers, trois responsabilités — c'est ce qui rend le système maintenable :

| Fichier | Contenu | Exemple |
|---|---|---|
| **Compte** (`accounts/`) | identifiant de connexion fourni par le propriétaire ; le mot de passe n'est **jamais** dedans, seulement le nom de la variable d'environnement | `accounts/example.account.json` |
| **Profil** (`profiles/`) | les données d'une personne (identité, adresse, photo, consentements) — utile seulement pour l'inscription | `profiles/example.profile.json` |
| **Recette** (`recipes/`) | le mode d'emploi du site : `login`, `steps`, `loop` | `recipes/demo_loop.json` |

Commandes :

| Commande | Rôle |
|---|---|
| `python -m signup_bot check --recipe recipes/demo_loop.json` | vérifie la recette (actions, domaines, boucle, hôtes de chaque `goto`) |
| `python -m signup_bot account --account accounts/example.account.json` | vérifie un compte |
| `python -m signup_bot validate --profile profiles/example.profile.json` | vérifie un profil + sa photo |
| `python -m signup_bot run … --dry-run` | rejoue toute la mécanique **hors ligne**, sans navigateur ni secret |
| `python -m signup_bot run …` | exécution réelle (`--headful` pour voir le navigateur) |
| `python -m signup_bot doctor` | vérifie que Playwright et le navigateur sont installés |

---

## 2. Le mode boucle, expliqué (le cœur du besoin)

### 2.1 Le principe

```jsonc
{
  "site": "mon-portail",
  "start_url": "https://mon-portail.example/espace",
  "allowed_hosts": ["mon-portail.example"],
  "authorized": true,                        // obligatoire

  "login": {                                 // 1. SE CONNECTER
    "url": "https://mon-portail.example/connexion",
    "steps": [
      {"action": "fill", "selector": "#username", "secret_field": "username"},
      {"action": "fill", "selector": "#password", "secret_field": "password"},
      {"action": "click", "selector": "button[type=submit]"}
    ],
    "success_when":  [{"kind": "selector", "selector": "#reserver"}],
    "logged_in_when":[{"kind": "selector", "selector": "#reserver"}],
    "reuse_session": true                    // évite de se reconnecter à chaque run
  },

  "loop": {                                  // 2. BOUCLER
    "reset_steps": [{"action": "goto", "value": "https://mon-portail.example/espace"}],
    "steps": [                               //    boutons prédéfinis, rejoués à chaque essai
      {"action": "click", "selector": "#reserver"}
    ],
    "retry_when":  [{"kind": "text", "contains": "Aucun créneau disponible"}],
    "success_when":[{"kind": "text", "contains": "Réservation confirmée"}],
    "stop_when":   [{"kind": "text", "contains": "Compte suspendu"}],

    "max_attempts": 10,                      // plafond dur
    "max_duration_seconds": 900,             // plafond de temps
    "interval_seconds": 5,                   // pause entre deux essais
    "backoff_factor": 1.2,                   // la pause s'allonge progressivement
    "jitter_seconds": 1                      // ± aléa : pas de cadence mécanique
  }
}
```

À chaque essai, le moteur fait exactement ceci :

```
        ┌─────────────────────────────────────────────────────────────┐
        │  reset_steps  (revenir à un état connu : la page d'accueil) │
        │        ▼                                                    │
        │  steps        (cliquer sur les boutons prédéfinis)          │
        │        ▼                                                    │
        │  LIRE LA PAGE, dans cet ordre :                             │
        │    1. success_when ?  ── oui ─►  SUCCÈS, on s'arrête         │
        │    2. stop_when ?     ── oui ─►  ARRÊT PROPRE                │
        │    3. retry_when ?    ── oui ─►  pause, puis essai suivant   │
        │    4. rien           ──►  ABANDON (page inattendue, on ne    │
        │                          clique pas au hasard)               │
        └─────────────────────────────────────────────────────────────┘
```

Trois issues possibles à retenir : **success**, **stopped** (arrêt demandé par une
condition ou par vous), **abandoned** (la page ne ressemble à rien de prévu : mieux
vaut s'arrêter que cliquer au hasard). S'y ajoutent **exhausted** (plafond d'essais
ou de durée) et **failed** (erreur technique : identifiants, page, navigateur).

Codes de sortie de la CLI : `0` succès, `3` objectif non atteint mais exécution
propre (arrêt/abandon/plafond), `1` échec technique, `2` erreur d'usage. Pratique
dans un cron ou un pipeline.

### 2.2 Les conditions : comment dire « si cette notification s'affiche »

Trois types suffisent dans la vraie vie :

| `kind` | Champ | Vrai quand… | Exemple |
|---|---|---|---|
| `text` | `contains` | le texte est visible dans la page | `"Aucun créneau disponible"` |
| `selector` | `selector` | un élément correspondant est visible | `"#reserver"`, `"text=Réservation confirmée"` |
| `url_contains` | `url` | l'URL courante contient la chaîne | `"/confirmation"` |

Une condition est une **observation**, jamais une action : elle ne clique pas, ne
remplit rien. Ajoutez `"description"` pour que les journaux et l'écran soient
lisibles (`"notification : aucun créneau"` plutôt que `text='Aucun créneau…'`).

### 2.3 Le débit est plafonné par conception

C'est le point qui distingue un outil sérieux d'un « bot » :

* **plancher de politesse** : impossible de descendre sous **15 s** entre deux essais
  sur un site distant (le plancher tombe à 1 s uniquement pour `127.0.0.1`/`localhost`,
  afin de tester). Une recette qui demande 2 s sur un vrai site est automatiquement
  remontée à 15 s ;
* **back-off** : la pause s'allonge (`interval × backoff_factor`) jusqu'à
  `max_interval_seconds` ;
* **jitter** : un aléa s'ajoute à chaque pause ;
* **plafonds** : `max_attempts` (≤ 500) et `max_duration_seconds`, vérifiés avant
  chaque essai ;
* **un run à la fois** par couple (compte, site) : `guardrails.RateLimiter` refuse
  une relance immédiate (état dans `artifacts/.state/`).

> Autrement dit : ce n'est pas un outil pour marteler un serveur. Pour du volume
> légitime (votre propre site), la bonne réponse est une **API** ou une file de
> messages côté serveur, pas 200 navigateurs.

### 2.4 Reprise, session et journal

* **Session réutilisée** : après une connexion réussie, les cookies sont stockés
  (`artifacts/.state/sessions/…`) ; le run suivant vérifie `logged_in_when` et saute
  la connexion si la session est encore valide.
* **Secrets** : `"secret_field": "username" | "password"` prend les identifiants du
  fichier de compte ; `"secret_env": "MON_SECRET"` prend n'importe quelle valeur dans
  l'environnement. **Les deux sont écrits `***` dans le journal.** Le mot de passe
  est vérifié *avant* d'ouvrir le navigateur.
* **Journal d'audit** (`artifacts/audit.jsonl`) : `run_started`, `login_started`,
  `login_succeeded`, `loop_attempt`, `condition_matched`, `loop_retry`, `sleep`,
  `loop_success`, `human_pause`, `run_finished`… Un événement par ligne, e-mail et
  téléphone masqués.
* **Notifications de fin** : `artifacts/notifications.jsonl` (toujours) + webhook
  optionnel si la variable `SIGNUP_WEBHOOK_URL` est définie — de quoi brancher
  Slack, Telegram ou n8n sans dépendance.
* **Captures d'écran** : `artifacts/<run_id>/failure.png` en cas d'échec.

### 2.5 Pièges réels (rencontrés pendant les tests)

1. **Texte trop générique** : le texte « Aucun créneau disponible » figurait aussi
   dans une phrase d'aide de la page d'accueil → `retry_when` se déclenchait à tort.
   Fixez la condition sur le message lui-même
   (`{"kind": "selector", "selector": "#message"}`) ou sur un texte unique, et
   vérifiez `success_when` en priorité (le moteur le fait déjà).
2. **Page de succès qui contient encore l'ancien message** (historique, bandeau) :
   l'ordre succès → arrêt → réessai évite la boucle infinie.
3. **Session expirée** : la page de connexion réapparaît. Sans `stop_when`, la
   boucle clique dans le vide. Ajoutez une condition d'arrêt du type
   `{"kind": "selector", "selector": "#username"}` (le formulaire de connexion est
   visible = session perdue → on s'arrête).
4. **Bouton qui devient inactif** après un clic : ajoutez `wait_for` sur l'état
   attendu avant le clic suivant plutôt que des `wait_ms` arbitraires.
5. **Double réservation** : un clic renvoyé deux fois (ou un run concurrent sur le
   même compte) crée deux réservations. Le verrou par compte et le plafond d'essais
   limitent le risque ; en production, ajoutez un contrôle « déjà réservé » en
   `stop_when`.

### 2.6 Adapter ce mode à votre site réel

1. Ouvrir la page dans Chrome → clic droit sur le bouton → **Inspecter** →
   `Copy → Copy selector`. Préférez `#id` ou `[name=...]`, jamais
   `.sc-bdVaJa > div:nth-child(3)`.
2. Noter le **texte exact** de la notification qui doit déclencher un nouvel essai
   (copier-coller depuis la page, pas de mémoire).
3. Noter le **texte ou l'élément** qui prouve le résultat voulu.
4. Remplir `recipes/mon-site.json` (copiez `recipes/demo_loop.json`), puis :
   `check` → `--dry-run` → `--headful` avec un compte de test → run réel.
5. Si le site change de formulaire, la recette échoue proprement avec
   `ABANDONED` et une capture : il suffit de mettre à jour le JSON.

---

## 3. Démarrage en 5 minutes

```bash
# 1. dépendances (le cœur ne demande que la bibliothèque standard)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # télécharge le navigateur (~150 Mo)

# 2. vérifier l'environnement et les fichiers
python -m signup_bot doctor
python -m signup_bot check   --recipe  recipes/demo_loop.json
python -m signup_bot account --account accounts/example.account.json

# 3. lancer le site de démonstration (autre terminal)
python -m demo.server            # http://127.0.0.1:8000  (alice / demo1234)

# 4. simuler, puis exécuter le mode boucle
export DEMO_PASSWORD='demo1234'
python -m signup_bot run --recipe recipes/demo_loop.json \
       --account accounts/example.account.json --dry-run
python -m signup_bot run --recipe recipes/demo_loop.json \
       --account accounts/example.account.json --headful
```

Le site de démo répond « Aucun créneau disponible pour le moment » aux **2 premiers**
essais, puis « Réservation confirmée ! » : le run doit donc finir en `SUCCESS` avec
**2 essais** et une pause entre les deux. Le parcours d'inscription existe aussi :

```bash
python -m signup_bot run --recipe recipes/demo_local.json \
       --profile profiles/example.profile.json --headful
```

Tests du noyau (aucune dépendance, aucun réseau) :

```bash
python -m unittest discover -s tests -v      # 45 tests
```

---

## 4. Architecture recommandée

### 4.1 Vue d'ensemble

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 1. SAISIE / STOCKAGE                                                     │
│    comptes (identifiants)   profils (PII)   photos   recettes (parcours) │
│    secrets dans le coffre / variables d'environnement — jamais sur disque│
└──────────────────────────────────┬───────────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 2. MÉTIER                                                                │
│    comptes.py : identifiant + nom de variable de mot de passe            │
│    models.py  : e-mail, E.164, date, image réelle (magic bytes), consent.│
│    recipes.py : actions typées, hôtes autorisés, cohérence de la boucle  │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 3. PARCOURS                                                              │
│    login → steps → loop{ reset_steps → steps → conditions }              │
│    conditions.py : text / selector / url_contains                        │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 4. ORCHESTRATION                                                         │
│    runner.py     : ordre succès → arrêt → réessai → abandon, reprise,    │
│                    session, secrets, journal                             │
│    guardrails.py : un run à la fois, planchers et plafonds de débit       │
│    notify.py     : outbox JSONL + webhook                                │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 5. EXÉCUTION                        adapters/ : Playwright | Mock | API   │
│    contrat commun → on remplace le navigateur sans réécrire la logique    │
└──────────────────────────────────────────────────────────────────────────┘
```

**Le point clé : séparation compte / profil / recette / moteur.** Les identifiants ne
sont pas le parcours ; le parcours n'est pas le programme. C'est ce qui permet de
tester sans navigateur, de changer d'outil (Selenium, Appium, API officielle) et
d'auditer ce qui a été fait.

### 4.2 Pourquoi déclaratif plutôt que du code par site ?

| Approche | Avantages | Inconvénients |
|---|---|---|
| Script Python par site | rapide à démarrer | duplication, non testable, secrets dans le code |
| **Recettes déclaratives (choisi)** | un fichier = un site, validable, relu par un non-développeur | limité aux actions connues (extensible) |
| LLM qui « regarde » la page et clique | souple sur sites inconnus | lent, coûteux, imprévisible — dangereux avec des identifiants réels |
| API officielle du site | le plus fiable et le plus rapide | n'existe pas toujours ; à privilégier quand elle existe |

> **API officielle > recette déclarative > LLM.** Un LLM est excellent pour *écrire*
> les sélecteurs d'une recette, mauvais pour *cliquer* en production.

### 4.3 Idempotence et reprise

* identifiant de run unique + dossier d'artefacts existant = refus de relancer ;
* `RateLimiter` empêche deux runs rapprochés pour le même couple (compte, site) ;
* `stop_when` permet un arrêt propre (compte bloqué, créneau fermé…) au lieu d'une
  boucle qui insiste ;
* une page inattendue provoque un abandon **explicite**, jamais des clics au hasard ;
* en cas d'échec : capture d'écran + dernière étape + raison dans le journal.

---

## 5. Les choix techniques, expliqués

| Besoin | Choix retenu | Pourquoi |
|---|---|---|
| Piloter un navigateur | **Playwright (Python, sync)** | auto-attente des sélecteurs, cookie/session gérés, `set_input_files` fiable, plus stable que Selenium sur les pages modernes |
| Parcours | **JSON déclaratif** | lisible, versionnable, validé avant exécution, zéro code par site |
| Conditions | **3 types seulement** | `text` / `selector` / `url_contains` couvrent la quasi-totalité des notifications |
| Secrets | **variables d'environnement + `secret_field`** | jamais dans un JSON, un log ou Git |
| Session | **`storage_state` Playwright** | évite N connexions inutiles (et les alertes « nouvel appareil ») |
| Journal | **JSONL append-only, PII masquées** | exploitable par `grep`/`jq`, probant en cas de litige |
| Tests | **`unittest` + `MockAdapter` (scénarios)** | CI sans navigateur, exécution en quelques dizaines de ms |
| Interface (phase 3) | **FastAPI + Jinja/HTMX ou React** | API claire, file d'attente, dashboard d'exécutions |

**Détails qui font la différence en pratique :**

* **Téléphone** en E.164 (`+213555123456`) + variante locale ; **photo** vérifiée par
  ses octets (pas par l'extension) et bornée à 5 Mo.
* **Préférer `#id`, `[name=…]`, `text=…`** aux sélecteurs structurels.
* **Textes de notification copiés depuis la page** (accents, espaces insécables) :
  une comparaison approximative est la première cause de boucle infinie.
* **`stop_when` toujours renseigné** : au minimum « compte bloqué » / « session
  expirée ». Sinon la boucle peut insister sur un état qui ne changera jamais —
  les plafonds l'arrêtent, mais autant s'arrêter proprement.
* **Zéro « mode furtif »** : pas de rotation de proxy ni de faux fingerprint. Si le
  site bloque, c'est le signal qu'il faut une autorisation ou une API.

---

## 6. Limites légales et éthiques (à lire avant tout)

Ce projet agit sur des comptes réels. Trois règles à ne pas franchir :

1. **Autorisation du propriétaire du compte.** Chaque compte est fourni par lui, et le
   fichier `accounts/*.json` doit rester hors du dépôt (`.gitignore`). Remplir un
   formulaire ou réserver au nom de quelqu'un sans son accord peut relever de
   l'usurpation d'identité et viole le RGPD (art. 5-6) / la loi 18-07 en Algérie.
2. **Autorisation du site.** Automatiser un service tiers est presque toujours
   interdit par ses CGU. Les recettes exigent `"authorized": true` et ne peuvent
   visiter que les domaines listés dans `allowed_hosts`. Usages légitimes : votre
   propre site, un site client, un extranet partenaire, un concours de démonstration.
3. **Pas de contournement technique.** Ne jamais résoudre un CAPTCHA
   automatiquement ni simuler une empreinte de navigateur pour tromper un anti-bot :
   dans plusieurs juridictions (France, art. 323-1 du Code pénal) c'est une atteinte
   à un système de traitement automatisé de données. Pour la charge sur votre propre
   site, utilisez k6 ou Locust sur un staging.

**Ce que le dépôt ne fera pas**, volontairement : remplir un CAPTCHA, marteler un
serveur (débit plafonné par conception), réutiliser les identifiants d'un tiers.
Un CAPTCHA ou un OTP déclenche une **pause humaine** (`pause_for_human`, mode
`--headful`) ou une demande d'accès à l'API officielle.

Données personnelles : profils réels hors du dépôt et chiffrés au repos en
production ; durée de conservation définie (purge des profils après usage) ; droit
d'accès/suppression = suppression du profil + purge des artefacts ; jamais de mot de
passe, d'OTP ni de contenu de photo dans les journaux.

---

## 7. Passer à l'échelle (production)

```
┌───────────┐   ┌──────────────┐   ┌──────────────┐   ┌─────────────────┐
│ Frontend  │──►│ API (FastAPI)│──►│ File (Redis/ │──►│ Workers         │
│ dashboard │   │ auth, CRUD,  │   │  RabbitMQ)   │   │ Playwright      │
│ comptes,  │   │ quotas       │   │              │   │ (1 conteneur    │
│ recettes  │   └──────┬───────┘   └──────────────┘   │  = 1 navigateur)│
└───────────┘          │                              └────────┬────────┘
                       ▼                                       ▼
              ┌─────────────────┐                    ┌────────────────────┐
              │ PostgreSQL      │                    │ Stockage objets    │
              │ profils chiffrés│                    │ photos + captures  │
              │ + audit         │                    │ (S3/MinIO, TTL)    │
              └─────────────────┘                    └────────────────────┘
```

* **Workers isolés** : un conteneur par exécution, `headless`, limites CPU/mémoire,
  `timeout` dur (120 s typique) ;
* **File d'attente** : retries avec back-off, priorité, **bouton « annuler »** qui
  positionne le `stop_requested` du runner (déjà prévu dans l'API) ;
* **Secrets** : Vault / AWS Secrets Manager, jamais dans la file ni dans les logs ;
* **Observabilité** : métriques par site (taux de succès, essais moyens, durée par
  étape), traces OpenTelemetry, alerte si `ABANDONED` augmente (= le site a changé) ;
* **Un run par compte** : verrou distribué par `account_id` — deux workers sur le
  même compte se marchent dessus (session invalidée, double réservation) ;
* **Conformité** : registre de traitement, base légale, DPO si volume important.

---

## 8. Feuille de route

| Phase | Contenu | Statut |
|---|---|---|
| 1 | Comptes/profils validés, recettes déclaratives, **mode boucle**, audit, notifications, site de démo, 45 tests | ✅ ce dépôt |
| 2 | Assistant de création de recette (ouvre la page, propose les sélecteurs, écrit le JSON) | à faire |
| 3 | API FastAPI + dashboard (comptes, recettes, exécutions, annulation, reprise) | à faire |
| 4 | File d'attente + workers conteneurisés + métriques | à faire |
| 5 | Connecteur API officiel (prioritaire sur le navigateur quand disponible) | à faire |

---

## Structure du dépôt

```
signup_bot/            moteur (aucune dépendance obligatoire)
├── accounts.py        comptes + secrets par variable d'environnement
├── models.py          profils + validation (E.164, photo, consentements)
├── conditions.py      text / selector / url_contains
├── recipes.py         recettes JSON (login, steps, loop) + rendu {{ variables }}
├── runner.py          connexion, boucle, ordre succès→arrêt→réessai→abandon
├── guardrails.py      débit : un run à la fois, planchers, plafonds
├── audit.py           journal JSONL, masquage des PII
├── notify.py          outbox JSONL + webhook optionnel
├── cli.py             check / account / validate / run / doctor
└── adapters/          base.py (contrat), playwright_adapter.py, mock_adapter.py
recipes/               demo_loop.json (boucle), demo_local.json (inscription),
                       template_site_autorise.json (à copier)
accounts/              example.account.json — les comptes réels sont gitignorés
profiles/              example.profile.json + photos/
demo/                  site de démonstration (inscription + connexion + boucle)
tests/                 45 tests, sans réseau, sans navigateur
docs/ARCHITECTURE.md   modèle de données, cycle d'exécution, exploitation
```

Licence : à définir par le propriétaire du dépôt.
