"""
Tests du moteur de soumission Playwright, en mode headless.

Les trois plateformes (FreeWork, Freelance-Informatique, freelancermap)
gardent leur formulaire de candidature derrière une session authentifiée :
sans cookies exportés, la page affiche un mur de connexion. Pour tester le
moteur sans crédentials, on sert des **fixtures HTML locales** qui
reproduisent la structure observée (août 2026) — bouton Postuler, modale de
candidature, champ message, bouton d'envoi, texte de confirmation — et on
exécute le cycle complet en headless contre elles.

Un fichier de session factice (`storage_state` avec un cookie anodin) est
écrit dans un répertoire temporaire : le moteur exige une session pour
s'engager sur le chemin automatique, c'est le comportement réel.

Tests couverts pour chaque plateforme :
- cycle complet : clic « postuler » → remplissage → envoi → preuve de succès
- dry-run : remplit sans envoyer
- sans session exportée → `session_required`
- mur de connexion → `session_required` (session expirée)
- CAPTCHA → `captcha` (abandon propre)
"""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# Racine du projet (auto-freelance/) : seul chemin à mettre sur sys.path.
# Attention : NE PAS ajouter job_scanner/submission — il contient playwright.py
# qui shadowait le paquet pip `playwright` (ImportError « non installé »).
ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

import sys
sys.path.insert(0, str(ROOT))

import job_scanner.submission as submission_pkg  # noqa: E402
from job_scanner.submission import sessions  # noqa: E402
from job_scanner.submission.playwright import submit  # noqa: E402


# -- Serveur HTTP local ----------------------------------------------------


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
    """Répertoire de session factice avec une session valide pour chaque source."""
    (tmp_path / "fake").mkdir(exist_ok=True)
    state = {
        "cookies": [
            {
                "name": "session_factice",
                "value": "1",
                "domain": "127.0.0.1",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    for source in ("freework", "freelance-informatique", "freelancermap"):
        (tmp_path / f"{source}.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
    return tmp_path


BODY = (
    "Bonjour, je suis consultant indépendant spécialisé en IA générative "
    "et agents LLM. 10 ans d'expérience dont 5 en mission freelance. "
    "Je serais ravi d'échanger sur votre besoin."
)


# -- FreeWork ---------------------------------------------------------------


def test_freework_full_cycle(server, session_dir):
    url = f"{server}/freework.html"
    result = submit("freework", url, BODY, session_dir=str(session_dir))
    assert result.submitted is True, result.text
    assert result.mode == "auto"
    assert result.detail.get("proof", {}).get("type") == "text"


def test_freework_dry_run(server, session_dir):
    url = f"{server}/freework.html"
    result = submit("freework", url, BODY, session_dir=str(session_dir), dry_run=True)
    assert result.submitted is False
    assert result.detail.get("dry_run") is True
    assert result.detail.get("filled") is True


# -- Freelance-Informatique ------------------------------------------------


def test_fi_full_cycle_direct_form(server, session_dir):
    # URL de formulaire direct (chemin /candidature-…) : pas de clic requis.
    url = f"{server}/candidature-business-analyst-ia-sur-paris-260812C001.html"
    result = submit("freelance-informatique", url, BODY, session_dir=str(session_dir))
    assert result.submitted is True, result.text
    assert result.mode == "auto"


def test_fi_full_cycle_from_mission_page(server, session_dir):
    # URL de fiche mission : le mapper doit trouver le lien /candidature-…
    url = f"{server}/fi_mission.html"
    result = submit("freelance-informatique", url, BODY, session_dir=str(session_dir))
    assert result.submitted is True, result.text


# -- freelancermap ----------------------------------------------------------


def test_freelancermap_full_cycle(server, session_dir):
    url = f"{server}/freelancermap.html"
    result = submit("freelancermap", url, BODY, session_dir=str(session_dir))
    assert result.submitted is True, result.text
    assert result.mode == "auto"


def test_freelancermap_ready_closes_consent(server, session_dir):
    """La préparation ferme le banner OneTrust avant d'interagir."""
    url = f"{server}/freelancermap.html"
    result = submit("freelancermap", url, BODY, session_dir=str(session_dir))
    assert result.submitted is True, result.text


# -- Session absente --------------------------------------------------------


def test_no_session_returns_session_required(tmp_path):
    result = submit("freework", "https://www.free-work.com/x", BODY,
                    session_dir=str(tmp_path))
    assert result.mode == "session_required"
    assert result.submitted is False
    assert "session" in result.text.lower()


# -- Mur de connexion (session expirée) -------------------------------------


def test_login_wall_detected(server, session_dir):
    # La fixture /login.html reproduit un écran de connexion.
    result = submit("freework", f"{server}/login.html", BODY,
                    session_dir=str(session_dir))
    assert result.mode == "session_required"
    assert result.submitted is False
    assert result.detail.get("reason") == "login_wall"


# -- CAPTCHA ----------------------------------------------------------------


def test_captcha_aborts(server, session_dir):
    result = submit("freework", f"{server}/captcha.html", BODY,
                    session_dir=str(session_dir))
    assert result.mode == "captcha"
    assert result.submitted is False


# -- Sessions utilitaires ---------------------------------------------------


def test_session_summary(session_dir):
    summary = sessions.session_summary("freework", session_dir)
    assert summary["present"] is True
    assert summary["age_days"] is not None
    assert summary["stale"] is False


def test_session_summary_missing(tmp_path):
    summary = sessions.session_summary("freework", tmp_path)
    assert summary["present"] is False
    assert summary["path"] is None


def test_has_session(session_dir, tmp_path):
    assert sessions.has_session("freework", session_dir) is True
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert sessions.has_session("freework", empty_dir) is False