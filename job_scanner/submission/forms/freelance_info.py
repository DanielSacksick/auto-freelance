"""
forms/freelance_info.py — Formulaire de candidature Freelance-Informatique.

Freelance-Informatique est du HTML classique (serveur-rendu). Constaté en
conditions réelles (août 2026) :

- Sur la fiche mission, le lien « Postuler » pointe vers
  `/candidature-<slug>-<reference>` (ex.
  `/candidature-business-analyst-ia-sur-paris-260812C001`).
- Sans session, ce chemin redirige vers `/connexion?p=<chemin candidature>` :
  le formulaire est **derrière connexion**. Avec une session, le formulaire
  s'affiche directement à cette URL.
- Le site intermédie : le formulaire porte le message de candidature, parfois
  des champs annexes (disponibilité, TJM).

Le mapper navigue donc directement vers l'URL de candidature construite depuis
la fiche mission. La preuve de succès est un texte de confirmation post-envoi
(« Votre candidature a été envoyée », « transmise au client »…).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import quote, urljoin, urlparse, parse_qs

from job_scanner.submission.forms.base import FormMapper
from job_scanner.submission.forms import register

logger = logging.getLogger(__name__)

# Chemin de candidature : /candidature-<slug>-<reference>
_CANDIDATURE_RE = re.compile(r"/candidature-[^/]+$")


@register
class FreelanceInfoForm(FormMapper):
    """Freelance-Informatique — HTML classique, formulaire à /candidature-<slug>."""

    key = "freelance-informatique"

    # Sur la fiche mission, le CTA « Postuler » est un lien (a.btn…).
    apply_button_selectors = (
        "a[href*='/candidature-']",
        "a:has-text('Postuler')",
    )
    message_selectors = (
        "textarea[name*='message' i], textarea[id*='message' i], "
        "textarea[placeholder*='message' i], textarea[placeholder*='motivation' i], "
        "textarea[placeholder*='présentation' i]",
        "form textarea",
    )
    submit_button_selectors = (
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Envoyer')",
        "button:has-text('Postuler')",
    )

    # Confirmation visible après envoi réussi.
    SUCCESS_TEXTS = (
        "votre candidature a été envoyée", "votre candidature a bien été envoyée",
        "candidature envoyée", "transmise au client", "candidature transmise",
        "merci pour votre candidature", "votre message a bien été envoyé",
    )

    def prepare(self, page: Any) -> None:
        """Ferme les éventuels banners de cookies (OneTrust, Axeptio…)."""
        for selector in (
            "#onetrust-accept-btn-handler",
            "button:has-text('Accepter')",
            "button:has-text('Tout accepter')",
            "button:has-text('Accept all')",
            "button:has-text('Accepter & Fermer')",
        ):
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    locator.click(timeout=3000)
                    logger.debug("freelance-info: banner cookies fermé")
                    return
            except Exception:
                continue

    def goto_application(self, page: Any, url: str) -> None:
        """
        Ouvre la fiche mission puis navigue vers son formulaire de candidature.

        Si `url` est déjà un chemin `/candidature-…`, on l'ouvre tel quel.
        Sinon on extrait le lien « Postuler » de la fiche (le plus fiable :
        le site construit lui-même le chemin avec la référence interne).
        """
        if _CANDIDATURE_RE.search(urlparse(url).path):
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return

        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        for selector in self.apply_button_selectors:
            locator = page.locator(selector).first
            try:
                if locator.count():
                    href = locator.get_attribute("href")
                    if href and "/candidature-" in href:
                        # Relatif à l'origine courante (jamais un domaine en dur :
                        # la fiche et le formulaire vivent sur le même site).
                        target = urljoin(page.url, href)
                        page.goto(target, wait_until="domcontentloaded", timeout=45000)
                        return
            except Exception:
                continue
        logger.info("freelance-info: pas de lien candidature trouvé sur la fiche")

    def find_apply_button(self, page: Any) -> Optional[Any]:
        """Déjà sur le formulaire (chemin /candidature-) : pas de bouton à cliquer."""
        if _CANDIDATURE_RE.search(urlparse(page.url).path):
            return None
        return super().find_apply_button(page)

    def success_proof(self, page: Any) -> Optional[Dict[str, Any]]:
        try:
            body = page.inner_text("body") or ""
        except Exception:
            body = ""
        for text in self.SUCCESS_TEXTS:
            if text in body.lower():
                return {"type": "text", "value": text}
        return None
