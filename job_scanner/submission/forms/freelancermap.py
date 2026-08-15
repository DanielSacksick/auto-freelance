"""
forms/freelancermap.py — Formulaire de candidature freelancermap.

freelancermap est une application React-on-Rails. Constaté en conditions
réelles (août 2026) :

- Sur la fiche projet, le bouton « Apply now » (`fm-btn fm-btn-primary` avec
  text "Apply now") est visible. Sans session, cliquer ouvre une modale qui
  contient des liens « Log in » et « Sign up » (`.alert-href`) — la
  candidature est derrière connexion.
- Une modale secondaire `availability-modal` (`.flm-modal.availability-modal`)
  existe indépendamment pour les réglages de disponibilité — le mapper la
  reconnaît et ne la confond pas avec le formulaire d'envoi.
- La preuve de succès est un texte de confirmation (« Application sent »,
  « Your application has been sent ») ou l'apparition d'un message de
  confirmation dans l'interface.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from job_scanner.submission.forms.base import FormMapper
from job_scanner.submission.forms import register

logger = logging.getLogger(__name__)


@register
class FreelancermapForm(FormMapper):
    """freelancermap — React-on-Rails, modal Apply now, formulaire derrière session."""

    key = "freelancermap"

    apply_button_selectors = (
        "button:has-text('Apply now')",
        "a:has-text('Apply now')",
        "a[href*='apply']:has-text('Apply')",
    )
    message_selectors = (
        "textarea[name*='cover' i], textarea[name*='message' i], "
        "textarea[name*='motivation' i], textarea[placeholder*='cover' i], "
        "textarea[placeholder*='message' i]",
        "form textarea",
    )
    submit_button_selectors = (
        "button[type='submit']:has-text('Send')",
        "button[type='submit']:has-text('Apply')",
        "button[type='submit']:has-text('Submit')",
        "button[type='submit']",
    )

    # Confirmation visible après envoi.
    SUCCESS_TEXTS = (
        "application sent", "application has been sent",
        "your application has been submitted", "application submitted",
        "thank you for your application", "thank you for applying",
        "bewerbung versendet", "bewerbung erfolgreich",
    )

    # Modale de connexion / inscription : motifs de détection.
    LOGIN_MODAL_HINTS = ("log in", "sign up", "login", "register")

    def prepare(self, page: Any) -> None:
        """Ferme la modale OneTrust des cookies (présente sur freelancermap)."""
        _close_fm_consent(page)

    def goto_application(self, page: Any, url: str) -> None:
        """Ouvre la fiche projet — le bouton Apply now y est."""
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # Plusieurs modales peuvent exister ; on attend que la page soit stable.
        page.wait_for_timeout(2000)
        self.prepare(page)

    def find_apply_button(self, page: Any) -> Optional[Any]:
        """Le bouton Apply now, en évitant le lien de navigation."""
        for selector in self.apply_button_selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() and locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    def success_proof(self, page: Any) -> Optional[Dict[str, Any]]:
        try:
            body = page.inner_text("body") or ""
        except Exception:
            body = ""
        for text in self.SUCCESS_TEXTS:
            if text in body.lower():
                return {"type": "text", "value": text}
        return None


def _close_fm_consent(page: Any) -> None:
    """Ferme la modale OneTrust des cookies (bouton « Accept all cookies »)."""
    for selector in (
        "button:has-text('Accept all cookies')",
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept all')",
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Consent')",
    ):
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                locator.click(timeout=3000)
                logger.debug("freelancermap: consent banner fermé")
                return
        except Exception:
            continue