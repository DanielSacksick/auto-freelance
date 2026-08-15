"""
playwright.py — Moteur Playwright générique de soumission de candidatures.

Remplace le mode « assisté » (copie presse-papier + ouverture navigateur)
par un remplissage automatique du formulaire, plateforme par plateforme.

Cycle complet :

1. Vérifie qu'une session authentifiée existe (`sessions.has_session`). Sans
   session, la soumission automatique est impossible : le moteur rend un
   résultat `session_required` et l'appelant (submit_application) retombe en
   mode assisté.
2. Ouvre le formulaire via le FormMapper de la plateforme.
3. Détecte les murs : page de connexion (session expirée) et CAPTCHA. Dans
   les deux cas on abandonne proprement — aucune soumission ne part.
4. Remplit le champ message, clique sur le bouton d'envoi.
5. Vérifie la preuve de succès : `applied` n'est écrit QUE si l'envoi est
   confirmé.

Sécurité : rythme humain (délais aléatoires entre actions), jamais de
double-clic, timeout global par étape. Le but est d'automatiser la
soumission, pas de se faire suspendre.

Usage CLI ::

    python -m job_scanner.submission.playwright submit \\
        --source freework --url <url> --body <texte>
    python -m job_scanner.submission.playwright --list
    python -m job_scanner.submission.playwright --sessions
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from typing import Any, Dict, Optional

from job_scanner.submission import sessions
from job_scanner.submission.forms import get_form, list_platforms, REGISTRY
from job_scanner.submission.forms.base import FormMapper, LOGIN_WALL_HINTS, CAPTCHA_HINTS, matches_hint

logger = logging.getLogger(__name__)

# Délais (secondes) entre actions, pour un rythme humain.
MIN_PAUSE, MAX_PAUSE = 0.8, 2.2

# Timeout global par étape (secondes).
STEP_TIMEOUT = 20000

# Motifs d'URL de connexion (session expirée).
LOGIN_URL_HINTS = ("/login", "/connexion", "/signin", "/log-in", "/se-connecter")


class SubmissionError(Exception):
    """Erreur de soumission propre, avec un message pour l'humain."""


class SubmissionResult:
    """Résultat d'une tentative de soumission automatique."""

    __slots__ = ("mode", "submitted", "text", "url", "detail")

    def __init__(self, mode: str, submitted: bool, text: str,
                 url: str = "", detail: Optional[Dict[str, Any]] = None):
        self.mode = mode          # auto | assisted | session_required | captcha | error
        self.submitted = submitted
        self.text = text
        self.url = url
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "submitted": self.submitted,
            "text": self.text,
            "url": self.url,
            "detail": self.detail,
        }


def _pause() -> None:
    """Pause courte et aléatoire : rythme humain, pas de rafale de clics."""
    time.sleep(random.uniform(MIN_PAUSE, MAX_PAUSE))


def submit(source: str, url: str, body: str, *,
           headless: bool = True,
           session_dir: Optional[str] = None,
           dry_run: bool = False) -> SubmissionResult:
    """
    Soumet une candidature sur une plateforme via Playwright.

    Args:
        source: clé de plateforme (freework, freelance-informatique, freelancermap).
        url: URL de la fiche mission.
        body: texte de la candidature (draft).
        headless: navigateur invisible (True par défaut — tests et cron).
        session_dir: répertoire des sessions exportées (défaut ~/.auto-freelance/sessions).
        dry_run: remplit le formulaire mais ne clique pas sur Envoyer.

    Returns:
        SubmissionResult — `submitted` n'est True que si la preuve de succès
        a été observée. Sinon `mode` indique pourquoi (session_required,
        captcha, error) et `submitted` reste False.
    """
    session_dir_path = None
    if session_dir:
        from pathlib import Path
        session_dir_path = Path(session_dir)

    if not sessions.has_session(source, session_dir_path):
        return SubmissionResult(
            mode="session_required", submitted=False,
            text=sessions.export_cookies_help(), url=url,
        )

    mapper = get_form(source)
    if mapper is None:
        return SubmissionResult(
            mode="error", submitted=False,
            text=f"❌ Plateforme « {source} » non cartographiée (formulaire inconnu).",
            url=url,
        )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return SubmissionResult(
            mode="error", submitted=False,
            text="❌ Playwright n'est pas installé dans ce venv. "
                 "Installe-le : pip install playwright && playwright install chromium",
            url=url,
        )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            state = sessions.load_storage_state(source, session_dir_path)
            context = browser.new_context(
                storage_state=state,
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"),
                locale="fr-FR" if source != "freelancermap" else "en-US",
            )
            page = context.new_page()
            return _run_submission(page, mapper, url, body, dry_run=dry_run)
        finally:
            browser.close()


def _run_submission(page: Any, mapper: FormMapper, url: str, body: str,
                    *, dry_run: bool) -> SubmissionResult:
    """Exécute le cycle complet sur une page ouverte."""
    try:
        mapper.prepare(page)
        mapper.goto_application(page, url)
        _pause()
    except Exception as exc:
        return SubmissionResult(
            mode="error", submitted=False,
            text=f"❌ Impossible d'ouvrir le formulaire ({exc})", url=url,
        )

    # 1. Mur de connexion (session expirée) ?
    if _on_login_page(page):
        return SubmissionResult(
            mode="session_required", submitted=False,
            text=("🔒 La session a expiré ou le compte n'est pas connecté. "
                  "Re-exporte la session : python -m job_scanner.submission.playwright "
                  "--export <source>"),
            url=page.url,
            detail={"reason": "login_wall"},
        )

    # 2. CAPTCHA ?
    if _detect_captcha(page):
        return SubmissionResult(
            mode="captcha", submitted=False,
            text=("🧩 CAPTCHA détecté sur la page. Soumission annulée pour ne pas "
                  "risquer un blocage de compte — validation manuelle requise."),
            url=page.url,
            detail={"reason": "captcha"},
        )

    # 3. Bouton « postuler » (si pas déjà sur le formulaire).
    apply_button = mapper.find_apply_button(page)
    if apply_button is not None:
        try:
            apply_button.click(timeout=STEP_TIMEOUT)
            _pause()
            page.wait_for_timeout(1500)
        except Exception as exc:
            # Fallback JS click pour les SPA (Nuxt 3, React) où le bouton
            # est dans le DOM mais pas visible/interactable.
            try:
                # Recherche par texte : :has-text() n'est pas du CSS pur,
                # document.querySelector ne le supporte pas. On scanne les
                # boutons et on clique celui dont le texte matche.
                for sel in mapper.apply_button_selectors:
                    # Extraire le texte recherché du sélecteur :has-text('X')
                    import re as _re
                    m = _re.search(r":has-text\('([^']+)'\)", sel)
                    if not m:
                        continue
                    target = m.group(1)
                    clicked = page.evaluate(f"""
                        (txt) => {{
                            const btns = document.querySelectorAll('button');
                            for (const b of btns) {{
                                if (b.textContent.trim().includes(txt)) {{
                                    b.click();
                                    return true;
                                }}
                            }}
                            return false;
                        }}
                    """, target)
                    if clicked:
                        break
                _pause()
                page.wait_for_timeout(1500)
                logger.info("playwright: clic«postuler» via JS fallback")
            except Exception as exc2:
                return SubmissionResult(
                    mode="error", submitted=False,
                    text=f"❌ Clic sur « postuler » impossible ({exc})", url=page.url,
                )
        # Le clic peut mener à une page de connexion ou un CAPTCHA.
        if _on_login_page(page):
            return SubmissionResult(
                mode="session_required", submitted=False,
                text=("🔒 La candidature demande une connexion (session absente "
                      "ou expirée). Re-exporte la session."),
                url=page.url, detail={"reason": "login_wall"},
            )
        if _detect_captcha(page):
            return SubmissionResult(
                mode="captcha", submitted=False,
                text="🧩 CAPTCHA détecté après clic sur « postuler ». Soumission annulée.",
                url=page.url, detail={"reason": "captcha"},
            )

    # 4. Remplissage du message.
    if not body.strip():
        return SubmissionResult(
            mode="error", submitted=False,
            text="❌ Aucune candidature à remplir (body vide)", url=page.url,
        )

    filled = mapper.fill_message(page, body)
    if not filled:
        return SubmissionResult(
            mode="error", submitted=False,
            text="❌ Champ message introuvable sur le formulaire — mapping à revoir "
                 "(la plateforme a peut-être changé son formulaire).",
            url=page.url, detail={"reason": "message_field_not_found"},
        )
    _pause()

    # 5. Envoi (sauf dry-run).
    submit_button = mapper.find_submit_button(page)
    if submit_button is None:
        return SubmissionResult(
            mode="error", submitted=False,
            text="❌ Bouton d'envoi introuvable — mapping à revoir.",
            url=page.url, detail={"reason": "submit_button_not_found"},
        )

    if dry_run:
        return SubmissionResult(
            mode="auto", submitted=False,
            text=f"🧪 Dry-run : formulaire rempli, envoi non cliqué (URL: {page.url})",
            url=page.url, detail={"dry_run": True, "filled": True},
        )

    try:
        submit_button.click(timeout=STEP_TIMEOUT)
    except Exception as exc:
        # Fallback JS click pour les SPA
        try:
            for sel in mapper.submit_button_selectors:
                import re as _re
                m = _re.search(r":has-text\('([^']+)'\)", sel)
                if not m:
                    continue
                target = m.group(1)
                clicked = page.evaluate(f"""
                    (txt) => {{
                        const btns = document.querySelectorAll('button');
                        for (const b of btns) {{
                            if (b.textContent.trim().includes(txt)) {{
                                b.click();
                                return true;
                            }}
                        }}
                        return false;
                    }}
                """, target)
                if clicked:
                    break
        except Exception as exc2:
            return SubmissionResult(
                mode="error", submitted=False,
                text=f"❌ Clic sur le bouton d'envoi impossible ({exc})",
                url=page.url,
            )

    # 6. Vérification : la preuve de succès doit être observée.
    page.wait_for_timeout(3000)
    proof = mapper.success_proof(page)
    if proof is None:
        return SubmissionResult(
            mode="error", submitted=False,
            text=("⚠️ Envoi cliqué mais aucune confirmation observée. "
                  "Vérifie manuellement avant de considérer la candidature envoyée."),
            url=page.url,
            detail={"reason": "no_success_proof", "last_url": page.url},
        )

    return SubmissionResult(
        mode="auto", submitted=True,
        text="✅ Candidature envoyée (confirmé par la plateforme).",
        url=page.url, detail={"proof": proof},
    )


# -- Détections ------------------------------------------------------------


def _on_login_page(page: Any) -> bool:
    """La page courante est-elle un écran de connexion ?"""
    url = page.url.lower()
    if any(hint in url for hint in LOGIN_URL_HINTS):
        return True
    try:
        body = (page.inner_text("body") or "")[:4000]
    except Exception:
        return False
    # Un formulaire avec un champ password est un mur de connexion.
    if page.locator("input[type='password']").count() > 0:
        return True
    return matches_hint(body, LOGIN_WALL_HINTS) and "password" in body.lower()


def _detect_captcha(page: Any) -> bool:
    """Un CAPTCHA (reCAPTCHA/hCaptcha) est-il présent sur la page ?"""
    # iframes de CAPTCHA
    for frame_selector in ("iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
                           "iframe[src*='captcha']"):
        if page.locator(frame_selector).count() > 0:
            return True
    try:
        body = (page.inner_text("body") or "")[:4000]
    except Exception:
        return False
    return matches_hint(body, CAPTCHA_HINTS)


# -- CLI -------------------------------------------------------------------


def _cmd_export(source: str) -> int:
    """Ouvre un navigateur visible pour exporter la session d'une plateforme."""
    from playwright.sync_api import sync_playwright

    mapper_ok = get_form(source) is not None
    if not mapper_ok:
        print(f"❌ Plateforme « {source} » inconnue. Connues : {', '.join(REGISTRY)}")
        return 1

    domains = sessions.SOURCE_DOMAINS
    domain = domains.get(source)
    url = f"https://{domain}" if domain else "https://www.google.com"
    path = sessions.session_path(source)

    print(f"🔓 Ouvre {url} — connecte-toi puis appuie sur Entrée…")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(locale="fr-FR")
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        input("Connecté ? Appuie sur Entrée pour exporter la session…")
        context.storage_state(path=str(path))
        browser.close()
    print(f"✅ Session exportée vers {path}")
    return 0


def _cmd_sessions() -> int:
    """Affiche l'état des sessions exportées, sans exposer leur contenu."""
    print("📂 Sessions exportées (~/.auto-freelance/sessions/)")
    found = False
    for source in sorted(sessions.SOURCE_DOMAINS):
        summary = sessions.session_summary(source)
        if not summary["present"]:
            print(f"  · {source:<24} ❌ absente")
            continue
        found = True
        stale = " ⚠️ périmée" if summary["stale"] else ""
        domain = "domaine OK" if summary["domain_ok"] else (
            "⚠️ domaine inattendu" if summary["domain_ok"] is False else "domaine non vérifié")
        print(f"  · {source:<24} ✅ {summary['age_days']} j{stale} — {domain}")
    if not found:
        print("\nAucune session exportée. Utilise --export <source> après connexion.")
    return 0


def _cmd_submit(args: argparse.Namespace) -> int:
    result = submit(
        args.source, args.url, args.body,
        headless=not args.visible,
        session_dir=args.session_dir,
        dry_run=args.dry_run,
    )
    print(result.text)
    return 0 if result.submitted else 1


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="job_scanner.submission.playwright",
        description="Soumission automatique de candidatures (Playwright).",
    )
    parser.add_argument("--list", action="store_true", help="liste les plateformes cartographiées")
    parser.add_argument("--sessions", action="store_true", help="état des sessions exportées")
    parser.add_argument("--export", metavar="SOURCE", help="exporte la session d'une plateforme")
    parser.add_argument("--dry-run", action="store_true", help="remplit sans envoyer")
    parser.add_argument("--visible", action="store_true", help="navigateur visible (débogage)")
    parser.add_argument("--session-dir", help="répertoire des sessions (défaut ~/.auto-freelance/sessions)")
    parser.add_argument("--source", help="clé plateforme")
    parser.add_argument("--url", help="URL de la fiche mission")
    parser.add_argument("--body", help="texte de la candidature")

    args = parser.parse_args(argv)

    if args.list:
        print("Plateformes cartographiées :")
        for key, doc in list_platforms().items():
            print(f"  · {key:<24} {doc}")
        return 0
    if args.sessions:
        return _cmd_sessions()
    if args.export:
        return _cmd_export(args.export)
    if args.source and args.url and args.body:
        return _cmd_submit(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
