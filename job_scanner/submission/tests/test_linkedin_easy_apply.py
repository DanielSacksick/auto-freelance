"""
tests/test_linkedin_easy_apply.py — moteur Playwright appliqué à
`job_scanner/submission/forms/linkedin.py` (Easy Apply multi-étapes).

Même approche que `test_playwright_submission.py` (fixture HTML locale servie
par un serveur HTTP local, cycle exécuté en headless réel) : ce fichier a ses
propres fixtures `server`/`session_dir` plutôt que d'en dépendre, ce dépôt ne
partage pas de `conftest.py`.

`fixtures/linkedin.html` reproduit une modale Easy Apply à 3 étapes
(téléphone -> message optionnel -> revue/envoi) avec le bouton final déjà
monté dans le DOM mais masqué avant l'étape 3 — le cas qui ferait échouer un
mapper qui ne vérifierait pas la visibilité (voir `LinkedInForm.
find_submit_button`). `fixtures/linkedin_blocked.html` ajoute une question de
sélection (case à cocher) que le mapper ne peut pas remplir sans deviner : la
candidature doit rester bloquée à l'étape 1, sans jamais cliquer Submit.
"""

from __future__ import annotations

import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(ROOT))

from job_scanner.submission.playwright import submit  # noqa: E402

BODY = (
    "Bonjour, je suis consultant indépendant spécialisé en IA générative "
    "et agents LLM. 10 ans d'expérience dont 5 en mission freelance. "
    "Je serais ravi d'échanger sur votre besoin."
)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass


@pytest.fixture(scope="module")
def server():
    handler = partial(_QuietHandler, directory=str(FIXTURES))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture()
def session_dir(tmp_path):
    """Session factice valide pour "linkedin"."""
    state = {
        "cookies": [
            {
                "name": "session_factice", "value": "1", "domain": "127.0.0.1",
                "path": "/", "expires": -1, "httpOnly": False, "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    (tmp_path / "linkedin.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


def test_linkedin_easy_apply_full_cycle(server, session_dir, monkeypatch):
    # AUTOFREELANCE_PHONE remplit l'étape 1 sans intervention manuelle.
    monkeypatch.setenv("AUTOFREELANCE_PHONE", "+33600000000")
    monkeypatch.delenv("AUTOFREELANCE_CV_PATH", raising=False)

    result = submit("linkedin", f"{server}/linkedin.html", BODY, session_dir=str(session_dir))

    assert result.submitted is True, result.text
    assert result.mode == "auto"
    assert result.detail.get("proof", {}).get("type") == "text"


def test_linkedin_easy_apply_dry_run(server, session_dir, monkeypatch):
    monkeypatch.setenv("AUTOFREELANCE_PHONE", "+33600000000")
    monkeypatch.delenv("AUTOFREELANCE_CV_PATH", raising=False)

    result = submit(
        "linkedin", f"{server}/linkedin.html", BODY,
        session_dir=str(session_dir), dry_run=True,
    )

    assert result.submitted is False
    assert result.detail.get("dry_run") is True
    assert result.detail.get("filled") is True


def test_linkedin_easy_apply_aborts_on_unanswerable_question(server, session_dir, monkeypatch):
    """Une question de sélection hors téléphone/CV/message ne doit jamais être
    devinée : la candidature reste bloquée, Submit n'est jamais cliqué, et le
    proof de succès (texte de confirmation) ne doit donc jamais apparaître."""
    monkeypatch.setenv("AUTOFREELANCE_PHONE", "+33600000000")
    monkeypatch.delenv("AUTOFREELANCE_CV_PATH", raising=False)

    result = submit(
        "linkedin", f"{server}/linkedin_blocked.html", BODY, session_dir=str(session_dir),
    )

    assert result.submitted is False
    assert result.mode == "error"


def test_linkedin_no_session_returns_session_required(tmp_path):
    result = submit("linkedin", "https://www.linkedin.com/jobs/view/1/", BODY,
                    session_dir=str(tmp_path))
    assert result.mode == "session_required"
    assert result.submitted is False


def test_linkedin_login_wall_detected(server, session_dir):
    # Fixture partagée avec test_playwright_submission.py : générique, ne
    # dépend d'aucune plateforme.
    result = submit("linkedin", f"{server}/login.html", BODY, session_dir=str(session_dir))
    assert result.mode == "session_required"
    assert result.submitted is False
    assert result.detail.get("reason") == "login_wall"


def test_linkedin_captcha_aborts(server, session_dir):
    result = submit("linkedin", f"{server}/captcha.html", BODY, session_dir=str(session_dir))
    assert result.mode == "captcha"
    assert result.submitted is False
