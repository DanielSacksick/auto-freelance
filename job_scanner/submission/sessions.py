"""
sessions.py — Sessions authentifiées des plateformes de candidature.

Chaque plateforme dispose d'un fichier de *storage state* Playwright
(`<source>.json`) : cookies + localStorage exportés depuis une session où
l'utilisateur est connecté. Le moteur Playwright charge ce fichier pour ouvrir
le formulaire de candidature déjà authentifié.

Export depuis une session existante ::

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False)
        ctx = b.new_context()
        page = ctx.new_page()
        page.goto("https://www.free-work.com/fr/login")
        input("Connecte-toi puis appuie sur Entrée…")
        ctx.storage_state(path="~/.auto-freelance/sessions/freework.json")
        b.close()

Règle de sûreté : un fichier de session est un secret au même titre qu'un mot
de passe — il ne doit jamais être commité, loggé, ni envoyé dans un chat.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Répertoire par défaut des sessions exportées.
DEFAULT_SESSION_DIR = Path(
    os.environ.get("AUTOFREELANCE_SESSION_DIR", "~/.auto-freelance/sessions")
).expanduser()

# Durée de fraîcheur d'une session : au-delà, on prévient mais on tente quand
# même (une session expirée est détectée au vol par le moteur, qui retombe en
# mode assisté sans faire de dégât).
STALE_DAYS = 14

# Domaines de référence pour vérifier qu'une session couvre la bonne plateforme.
SOURCE_DOMAINS = {
    "freework": "free-work.com",
    "freelance-informatique": "freelance-informatique.fr",
    "freelancermap": "freelancermap.com",
    "linkedin": "linkedin.com",
}


def session_path(source: str, session_dir: Optional[Path] = None) -> Path:
    """Chemin du fichier de session pour une source."""
    base = Path(session_dir) if session_dir else DEFAULT_SESSION_DIR
    return base / f"{source}.json"


def has_session(source: str, session_dir: Optional[Path] = None) -> bool:
    """Une session exportée existe-t-elle pour cette source ?"""
    return session_path(source, session_dir).is_file()


def load_storage_state(source: str, session_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Charge le storage state Playwright d'une source ; lève FileNotFoundError si absent."""
    path = session_path(source, session_dir)
    with open(path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    if not isinstance(state, dict):
        raise ValueError(f"Session {path} invalide : objet JSON attendu")
    return state


def session_summary(source: str, session_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Décrit une session sans exposer son contenu : présence, âge, domaine couvert.

    Returns:
        {"present": bool, "path": str|None, "age_days": float|None, "stale": bool,
         "domain_ok": bool|None}
    """
    path = session_path(source, session_dir)
    if not path.is_file():
        return {"present": False, "path": None, "age_days": None,
                "stale": None, "domain_ok": None}

    stat = path.stat()
    age_days = (os.path.getmtime(path) - stat.st_ctime)  # placeholder, calculé après
    import time
    age_days = (time.time() - stat.st_mtime) / 86400

    domain_ok: Optional[bool] = None
    try:
        state = load_storage_state(source, session_dir)
        cookies = state.get("cookies", [])
        wanted = SOURCE_DOMAINS.get(source)
        domain_ok = any(
            wanted and (c.get("domain", "").endswith(wanted) or wanted in c.get("domain", ""))
            for c in cookies
        ) if wanted else None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("sessions: %s illisible (%s)", path, exc)
        domain_ok = False

    return {
        "present": True,
        "path": str(path),
        "age_days": round(age_days, 1),
        "stale": age_days > STALE_DAYS,
        "domain_ok": domain_ok,
    }


def export_cookies_help() -> str:
    """Texte d'aide pour exporter une session, affiché quand aucune n'existe."""
    return (
        "Aucune session exportée pour cette plateforme. Exporte-la d'abord :\n"
        "  1. Ouvre un navigateur, connecte-toi à la plateforme\n"
        "  2. Lance : python -m job_scanner.submission.playwright --export freework\n"
        "     (le navigateur s'ouvre, connecte-toi, puis le fichier est écrit)\n"
        "Le fichier est stocké dans ~/.auto-freelance/sessions/<source>.json\n"
        "Tant qu'il n'existe pas, la soumission reste en mode assisté."
    )
