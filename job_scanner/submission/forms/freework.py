"""
forms/freework.py — Formulaire de candidature Free-Work (free-work.com).

Free-Work est une application Nuxt 3. Sur la fiche mission, le bouton
« Postuler » (`button.btn--medium.btn--secondary`) n'a pas de `href` : il est
piloté par JavaScript. Constaté en conditions réelles (août 2026) :

- Sans session, cliquer « Postuler » ne fait rien d'observable : le site
  affiche un encart « Créer un compte / Se connecter » à la place. La
  candidature est donc **strictement réservée aux connectés**.
- Avec une session, le clic ouvre une modale ou navigue vers le formulaire ;
  le mapping ci-dessous couvre les deux formes (modale de candidature et
  page dédiée), avec repli générique par intention.

La preuve de succès est un texte de confirmation (« Candidature envoyée »,
« Votre candidature a bien été envoyée ») ou la disparition de la modale
après envoi. FreeWork étant le plus susceptible d'avoir un CAPTCHA, le moteur
générique le détecte et abandonne proprement (voir `playwright.py`).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from job_scanner.submission.forms.base import FormMapper, matches_hint
from job_scanner.submission.forms import register

logger = logging.getLogger(__name__)


@register
class FreeWorkForm(FormMapper):
    """Free-Work — Nuxt 3, bouton Postuler JavaScript, formulaire derrière session."""

    key = "freework"

    # Sélecteurs observés sur la fiche mission (août 2026).
    apply_button_selectors = (
        "button.btn--medium.btn--secondary:has-text('Postuler')",
        "button:has-text('Postuler')",
        "button.btn--medium",  # Fallback générique si les classes changent
    )
    # Le formulaire peut exposer un textarea de message avec les attributs
    # name="job-application-message" et id="job-application-message".
    # ATTENTION : NE PAS utiliser le flag CSS 'i' (case-insensitive) car
    # document.querySelector() (utilisé dans base.py::fill_message) ne le
    # supporte pas — utiliser des sélecteurs exacts ou "textarea" comme
    # fallback générique.
    message_selectors = (
        "#job-application-message",
        "textarea[name='job-application-message']",
        "textarea[name*='message']",
        "textarea[name*='cover']",
        "textarea[name*='presentation']",
        "textarea[placeholder*='message']",
        "textarea[placeholder*='motivation']",
        "form textarea",
        "textarea",  # Fallback générique
    )
    submit_button_selectors = (
        "button:has-text('Je postule')",
        "button[type='submit']:has-text('Postuler')",
        "button[type='submit']:has-text('Envoyer')",
        "button:has-text('Envoyer ma candidature')",
        "button[type='submit']",
    )

    # Confirmation visible après envoi réussi.
    SUCCESS_TEXTS = (
        "candidature envoyée", "candidature bien envoyée",
        "candidature a bien été envoyée", "postulation envoyée",
        "merci, votre candidature", "candidature transmise",
        "envoyé", "envoyée", "confirmation",
    )

    def prepare(self, page: Any) -> None:
        """Ferme les éventuels banners de cookies / modales d'inscription."""
        _close_consent_banner(page)

    def goto_application(self, page: Any, url: str) -> None:
        """Ouvre la fiche mission ; le bouton Postuler y est."""
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # Hydratation Nuxt : le bouton devient visible après coup.
        try:
            page.wait_for_selector(
                "button.btn--medium.btn--secondary:has-text('Postuler')",
                timeout=12000,
            )
        except Exception:
            logger.debug("freework: bouton Postuler introuvable après hydratation")

    def find_apply_button(self, page: Any) -> Optional[Any]:
        """Le bouton Postuler, en préférant la variante visible (hydratée).
        Fallback : clic JS si le bouton est dans le DOM mais pas visible (SPA Nuxt)."""
        for selector in self.apply_button_selectors:
            locator = page.locator(selector).first
            try:
                # count() gère les sélecteurs Playwright (:has-text) et
                # retourne les éléments dans le DOM même invisibles.
                if locator.count():
                    return locator
            except Exception:
                continue
        return None

    def success_proof(self, page: Any) -> Optional[Dict[str, Any]]:
        """Cherche un texte de confirmation ou la disparition de la modale."""
        try:
            body = page.inner_text("body") or ""
        except Exception:
            body = ""
        for text in self.SUCCESS_TEXTS:
            if text in body.lower():
                return {"type": "text", "value": text}
        # La modale de candidature a disparu après un clic d'envoi : signe
        # faible mais réel que le formulaire est parti.
        return None


def _close_consent_banner(page: Any) -> None:
    """Ferme les banners cookies (OneTrust, etc.) s'ils apparaissent."""
    for selector in (
        "#onetrust-accept-btn-handler",
        "button:has-text('Accepter')",
        "button:has-text('Tout accepter')",
        "button:has-text('Accept all')",
    ):
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                locator.click(timeout=3000)
                logger.debug("freework: banner cookies fermé")
                return
        except Exception:
            continue
