"""Site de démonstration local : formulaire d'inscription + parcours « boucle ».

But : pouvoir tester le robot de bout en bout SANS toucher à un vrai site.

    python -m demo.server            # http://127.0.0.1:8000/

Parcours disponibles
--------------------
  /inscription   inscription (nom, adresse, téléphone, photo…)   → recipe demo_local
  /connexion     connexion au compte (démo : alice / demo1234)    → recipe demo_loop
  /espace        bouton « Réserver un créneau » qui échoue N fois avant de réussir

Les N premiers essais renvoient « Aucun créneau disponible pour le moment »
(c'est la « notification » qui déclenche un nouvel essai côté robot), les suivants
renvoient « Réservation confirmée ». Réglage : DEMO_FAILS=2 (par défaut),
DEMO_USER=alice, DEMO_PASSWORD=demo1234.

Aucune donnée n'est transmise : tout reste dans demo/submissions/ (ignoré par Git).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SUBMISSIONS = Path(__file__).parent / "submissions"
DEMO_USER = os.environ.get("DEMO_USER", "alice")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "demo1234")
DEMO_FAILS = int(os.environ.get("DEMO_FAILS", "2"))

STYLE = """
  body { font-family: system-ui, sans-serif; max-width: 560px; margin: 3rem auto; padding: 0 1rem; }
  label { display: block; margin-top: .8rem; font-weight: 600; }
  input { width: 100%; padding: .5rem; margin-top: .2rem; box-sizing: border-box; }
  button { margin-top: 1.2rem; padding: .7rem 1.2rem; font-weight: 700; cursor: pointer; }
  .hint { color: #666; font-size: .85rem; }
  .error { color: #b00020; font-weight: 600; }
  .card { border: 1px solid #ddd; border-radius: 10px; padding: 1.2rem; margin-top: 1.2rem; }
"""

FORM_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<title>Créer un compte — démo</title><style>{STYLE}</style></head>
<body>
<h1>Créer un compte</h1>
{error}
<form method="post" action="/inscription" enctype="multipart/form-data">
  <label for="first_name">Prénom</label>
  <input id="first_name" name="first_name" autocomplete="given-name" required>

  <label for="last_name">Nom</label>
  <input id="last_name" name="last_name" autocomplete="family-name" required>

  <label for="email">E-mail</label>
  <input id="email" name="email" type="email" required>

  <label for="phone">Téléphone</label>
  <input id="phone" name="phone" type="tel" placeholder="+213555123456" required>

  <label for="birthdate">Date de naissance</label>
  <input id="birthdate" name="birthdate" type="date" required>

  <label for="address_line1">Adresse</label>
  <input id="address_line1" name="address_line1" required>

  <label for="postal_code">Code postal</label>
  <input id="postal_code" name="postal_code" required>

  <label for="city">Ville</label>
  <input id="city" name="city" required>

  <label for="country">Pays (code ISO)</label>
  <input id="country" name="country" value="DZ" required>

  <label for="password">Mot de passe</label>
  <input id="password" name="password" type="password" required>

  <label for="photo">Photo de profil (JPEG/PNG, max 5 Mo)</label>
  <input id="photo" name="photo" type="file" accept="image/jpeg,image/png" required>

  <label><input type="checkbox" id="terms" name="terms" value="on" style="width:auto">
    J'accepte les conditions d'utilisation (démo)</label>

  <p class="hint">Site factice fourni avec le projet : aucune donnée n'est transmise.</p>
  <button type="submit">Créer mon compte</button>
</form>
</body></html>
"""

SUCCESS_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>Inscription confirmée</title>
<style>{STYLE}</style></head>
<body><h1>Inscription confirmée</h1>
<p>Bienvenue {first_name} {last_name} !</p>
<p>Compte de démonstration créé le {created_at}.</p>
<p>Identifiant interne : <code>{signup_id}</code></p>
</body></html>
"""

LOGIN_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>Connexion — démo</title>
<style>{STYLE}</style></head>
<body>
<h1>Connexion</h1>
{error}
<form method="post" action="/connexion">
  <label for="username">Identifiant</label>
  <input id="username" name="username" autocomplete="username" required>

  <label for="password">Mot de passe</label>
  <input id="password" name="password" type="password" required>

  <button type="submit">Se connecter</button>
  <p class="hint">Compte de démonstration : alice / demo1234</p>
</form>
</body></html>
"""

SPACE_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>Mon espace — démo</title>
<style>{STYLE}</style></head>
<body>
<h1>Mon espace</h1>
<p>Connecté en tant que <strong>{username}</strong>.</p>
<div class="card">
  <p>Créneaux disponibles : <strong>{slots}</strong></p>
  <form method="post" action="/reserver">
    <button type="submit" id="reserver">Réserver un créneau</button>
  </form>
  <p class="hint">Essais déjà effectués : {attempts} — les {fails} premiers essais
     échouent volontairement (page d'échec de réservation).</p>
</div>
<form method="post" action="/deconnexion"><button type="submit">Se déconnecter</button></form>
</body></html>
"""

RETRY_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>Aucun créneau</title>
<style>{STYLE}</style></head>
<body>
<h1>Réserver un créneau</h1>
<div class="card" id="resultat">
  <p id="message">Aucun créneau disponible pour le moment.</p>
  <p class="hint">Réessayez plus tard.</p>
</div>
<p><a href="/espace">Retour à mon espace</a></p>
</body></html>
"""

RESERVED_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>Réservation confirmée</title>
<style>{STYLE}</style></head>
<body>
<h1>Réserver un créneau</h1>
<div class="card" id="resultat">
  <p id="message">Réservation confirmée !</p>
  <p>Référence : <code>{reference}</code> — créneau du {slot}.</p>
</div>
<p><a href="/espace">Retour à mon espace</a></p>
</body></html>
"""

REQUIRED = [
    "first_name", "last_name", "email", "phone", "birthdate",
    "address_line1", "postal_code", "city", "country", "password",
]

SESSIONS: dict[str, str] = {}
ATTEMPTS: dict[str, int] = {}


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Analyse un corps multipart/form-data sans dépendance externe."""
    raw = b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    message = BytesParser(policy=email_policy).parsebytes(raw)
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files[name] = (filename, payload)
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


def fill(template: str, **values: object) -> str:
    """Remplacement de jetons (pas str.format : le CSS contient des accolades)."""
    page = template.replace("{STYLE}", STYLE)
    for key, value in values.items():
        page = page.replace("{" + key + "}", str(value))
    return page


class Handler(BaseHTTPRequestHandler):
    server_version = "DemoSite/0.2"

    # ------------------------------------------------------------------ outils
    def _send(self, status: int, html: str, headers: dict[str, str] | None = None) -> None:
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}

    def _current_user(self) -> str | None:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "demo_session" and value in SESSIONS:
                return SESSIONS[value]
        return None

    # -------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802 - API http.server
        path = urlparse(self.path).path
        if path in ("/", "/inscription"):
            self._send(200, fill(FORM_HTML, error=""))
        elif path == "/connexion":
            self._send(200, fill(LOGIN_HTML, error=""))
        elif path == "/espace":
            user = self._current_user()
            if user is None:
                self._redirect("/connexion")
                return
            attempts = ATTEMPTS.get(user, 0)
            self._send(200, fill(
                SPACE_HTML, username=user, attempts=attempts, fails=DEMO_FAILS,
                slots=max(1, DEMO_FAILS + 1 - attempts),
            ))
        else:
            self._send(404, "<h1>404</h1>")

    # ------------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/inscription":
            self._post_inscription()
        elif path == "/connexion":
            self._post_login()
        elif path == "/reserver":
            self._post_reserver()
        elif path == "/deconnexion":
            self._redirect("/connexion", {"Set-Cookie": "demo_session=; Max-Age=0; Path=/"})
        else:
            self._send(404, "<h1>404</h1>")

    def _post_inscription(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        content_type = self.headers.get("Content-Type", "")
        body = self.rfile.read(length)
        fields, files = parse_multipart(body, content_type) if "multipart/form-data" in content_type else ({}, {})

        missing = [name for name in REQUIRED if not fields.get(name, "").strip()]
        if "photo" not in files:
            missing.append("photo")
        if fields.get("terms") != "on":
            missing.append("terms (consentement)")
        if missing:
            self._send(400, fill(FORM_HTML, error=(
                '<p class="error">Champs manquants : ' + ", ".join(missing) + "</p>"
            )))
            return

        signup_id = uuid.uuid4().hex[:10]
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        SUBMISSIONS.mkdir(parents=True, exist_ok=True)
        filename, payload = files["photo"]
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
        (SUBMISSIONS / f"{signup_id}-{safe}").write_bytes(payload)
        record = {
            "signup_id": signup_id,
            "created_at": created_at,
            "fields": {k: v for k, v in fields.items() if k != "password"},
            "password_received": bool(fields.get("password")),
            "photo_bytes": len(payload),
        }
        (SUBMISSIONS / f"{signup_id}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._send(200, fill(
            SUCCESS_HTML,
            first_name=fields.get("first_name", ""),
            last_name=fields.get("last_name", ""),
            created_at=created_at,
            signup_id=signup_id,
        ))

    def _post_login(self) -> None:
        fields = self._form()
        username = fields.get("username", "").strip()
        password = fields.get("password", "")
        if username != DEMO_USER or password != DEMO_PASSWORD:
            self._send(401, fill(
                LOGIN_HTML,
                error='<p class="error" id="erreur">Identifiant ou mot de passe incorrect.</p>',
            ))
            return
        token = uuid.uuid4().hex
        SESSIONS[token] = username
        ATTEMPTS.setdefault(username, 0)
        self._redirect(
            "/espace", {"Set-Cookie": f"demo_session={token}; Path=/; HttpOnly; SameSite=Lax"}
        )

    def _post_reserver(self) -> None:
        user = self._current_user()
        if user is None:
            self._redirect("/connexion")
            return
        ATTEMPTS[user] = ATTEMPTS.get(user, 0) + 1
        attempt = ATTEMPTS[user]
        if attempt <= DEMO_FAILS:
            self._send(200, fill(RETRY_HTML))
        else:
            self._send(200, fill(
                RESERVED_HTML,
                reference=uuid.uuid4().hex[:8].upper(),
                slot="lundi 26 octobre, 09:30",
            ))

    def log_message(self, fmt: str, *args) -> None:  # log compact
        print(f"[demo] {self.address_string()} {fmt % args}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Site de démonstration (inscription + boucle)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Site de démo      : http://{args.host}:{args.port}/inscription")
    print(f"Connexion / boucle: http://{args.host}:{args.port}/connexion "
          f"({DEMO_USER} / {DEMO_PASSWORD}, {DEMO_FAILS} échecs simulés)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
