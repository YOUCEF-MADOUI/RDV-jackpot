"""Site de démonstration local : un formulaire d'inscription factice.

But : pouvoir tester le robot de bout en bout SANS toucher à un vrai site.
Lancement :
    python -m demo.server            # http://127.0.0.1:8000/inscription

Les soumissions sont écrites dans demo/submissions/ (ignoré par Git).
Ce site n'a aucune sécurité : il est fait pour être automatisé.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SUBMISSIONS = Path(__file__).parent / "submissions"

FORM_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<title>Créer un compte — démo</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 560px; margin: 3rem auto; padding: 0 1rem; }
  label { display: block; margin-top: .8rem; font-weight: 600; }
  input { width: 100%; padding: .5rem; margin-top: .2rem; box-sizing: border-box; }
  button { margin-top: 1.2rem; padding: .7rem 1.2rem; font-weight: 700; cursor: pointer; }
  .hint { color: #666; font-size: .85rem; }
  .error { color: #b00020; font-weight: 600; }
</style></head>
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
<html lang="fr"><head><meta charset="utf-8"><title>Inscription confirmée</title></head>
<body style="font-family: system-ui; max-width: 560px; margin: 3rem auto">
<h1>Inscription confirmée</h1>
<p>Bienvenue {first_name} {last_name} !</p>
<p>Compte de démonstration créé le {created_at}.</p>
<p>Identifiant interne : <code>{signup_id}</code></p>
</body></html>
"""

REQUIRED = [
    "first_name",
    "last_name",
    "email",
    "phone",
    "birthdate",
    "address_line1",
    "postal_code",
    "city",
    "country",
    "password",
]


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


class Handler(BaseHTTPRequestHandler):
    server_version = "DemoSignup/0.1"

    def _send(self, status: int, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - API http.server
        if self.path.split("?")[0] in ("/", "/inscription"):
            self._send(200, FORM_HTML.replace("{error}", ""))
        else:
            self._send(404, "<h1>404</h1>")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/inscription":
            self._send(404, "<h1>404</h1>")
            return
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
            error = (
                '<p class="error">Champs manquants : '
                + ", ".join(missing)
                + "</p>"
            )
            self._send(400, FORM_HTML.replace("{error}", error))
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
        # Remplacement de jetons (pas str.format : le CSS contient des accolades)
        page = (
            SUCCESS_HTML.replace("{first_name}", fields.get("first_name", ""))
            .replace("{last_name}", fields.get("last_name", ""))
            .replace("{created_at}", created_at)
            .replace("{signup_id}", signup_id)
        )
        self._send(200, page)

    def log_message(self, fmt: str, *args) -> None:  # log compact
        print(f"[demo] {self.address_string()} {fmt % args}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Formulaire d'inscription de démonstration")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Formulaire de démo : http://{args.host}:{args.port}/inscription")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
