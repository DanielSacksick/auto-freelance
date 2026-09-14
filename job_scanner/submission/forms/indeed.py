"""
forms/indeed.py — Formulaire de candidature Indeed.

**Non vérifié en conditions réelles au-delà de la page de recherche** :
Indeed applique une détection anti-bot agressive qui redirige toute session
anonyme/automatisée (même en simple lecture d'une fiche mission, sans aucune
action de candidature) vers un mur de connexion
(`secure.indeed.com/auth;jsessionid=…&from=bot-detection-anonymous`, constaté
le 01/09/2026). Impossible donc d'observer le DOM réel du formulaire, avec ou
sans session, dans cet environnement. Ce mapper est construit sur la
structure publique et documentée du widget « Indeed Apply » (bouton
`#indeedApplyButton`, modale dans une iframe `#indeedapply-modal-iframe`) —
**à valider manuellement avec une session réelle avant tout usage en
auto-submit** (voir `SESSION_REQUIREMENTS.md`). Tant qu'il n'est pas validé,
`indeed` reste hors de `config.AUTO_SUBMIT_PLATFORMS`.

Deux formes de candidature coexistent sur Indeed, jamais mélangées sur une
même offre :

- **Indeed Apply (interne)** — bouton `#indeedApplyButton` ; ouvre une modale
  en iframe qui progresse en plusieurs étapes (CV, questions, message,
  vérification) avant un écran final d'envoi. Automatisable, dans les mêmes
  limites de sûreté que LinkedIn Easy Apply (voir `forms/linkedin.py`) :
  toute question à laquelle le mapper ne sait pas répondre avec certitude
  fait abandonner proprement.
- **Candidature externe** — lien « Postuler sur le site de l'entreprise »
  qui redirige vers un site tiers arbitraire, hors périmètre : aucun
  `FormMapper` ne peut le suivre en toute sécurité, exactement comme le
  bouton « Apply » externe de LinkedIn. Ce mapper ne cible donc *que* le
  bouton Indeed Apply interne dans `apply_button_selectors` : sur une offre
  purement externe, `find_apply_button` ne trouve rien et le cycle s'arrête
  proprement (pas de candidature à moitié remplie).
"""

from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, Optional

from job_scanner.submission.forms.base import FormMapper, matches_hint
from job_scanner.submission.forms import register

logger = logging.getLogger(__name__)

MIN_STEP_PAUSE, MAX_STEP_PAUSE = 0.8, 2.0
MAX_STEPS = 8

# La modale Indeed Apply vit dans une iframe — tout le remplissage doit
# passer par un `frame_locator`, jamais par `page.locator()` directement.
IFRAME_SELECTOR = "iframe[id*='indeedapply-modal-iframe' i], iframe[title*='indeed apply' i]"

NEXT_HINTS = ("next", "suivant", "continue", "continuer")
REVIEW_HINTS = ("review", "verifier", "vérifier")

_KNOWN_FIELD_MARKERS = ("phone", "telephone", "téléphone", "message")


def _pause() -> None:
    time.sleep(random.uniform(MIN_STEP_PAUSE, MAX_STEP_PAUSE))


def _first_visible(locator: Any) -> Optional[Any]:
    try:
        count = locator.count()
    except Exception:
        return None
    for i in range(count):
        element = locator.nth(i)
        try:
            if element.is_visible():
                return element
        except Exception:
            continue
    return None


@register
class IndeedForm(FormMapper):
    """Indeed — Indeed Apply (iframe multi-étapes) ; candidature externe hors périmètre."""

    key = "indeed"

    # Uniquement le bouton Indeed Apply interne — jamais le lien de
    # candidature externe (voir docstring module).
    apply_button_selectors = (
        "#indeedApplyButton",
        "button:has-text('Postuler maintenant')",
        "button:has-text('Easily apply')",
    )
    submit_button_selectors = (
        "button:has-text('Submit your application')",
        "button:has-text('Envoyer ma candidature')",
        "button:has-text('Postuler')",
    )
    message_selectors = (
        "textarea[id*='message' i]",
        "textarea[name*='message' i]",
        "textarea",
    )

    SUCCESS_TEXTS = (
        "application submitted", "candidature envoyée", "candidature transmise",
        "votre candidature a été envoyée", "application sent",
    )

    def __init__(self) -> None:
        self._phone = os.environ.get("AUTOFREELANCE_PHONE", "").strip()
        cv_path = os.environ.get("AUTOFREELANCE_CV_PATH", "").strip()
        self._cv_path = Path(cv_path).expanduser() if cv_path else None

    # -- Cycle de vie --------------------------------------------------------

    def prepare(self, page: Any) -> None:
        for selector in (
            "#onetrust-accept-btn-handler",
            "button:has-text('Tout accepter')",
            "button:has-text('Accept all')",
        ):
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    locator.click(timeout=3000)
                    logger.debug("indeed: banner cookies fermé")
                    return
            except Exception:
                continue

    def goto_application(self, page: Any, url: str) -> None:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        _pause()

    def fill_message(self, page: Any, body: str) -> bool:
        """Absorbe la progression Indeed Apply dans l'iframe modale, jusqu'à
        l'écran final d'envoi. Mêmes règles de sûreté que LinkedIn Easy
        Apply : abandon net sur toute question non automatisable."""
        try:
            frame = self._frame(page)
            if frame is None:
                return False

            for _ in range(MAX_STEPS):
                self._fill_known_fields(frame, body)

                blocker = self._unanswerable_field(frame)
                if blocker is not None:
                    logger.info(
                        "indeed: question non automatisable (%s) — "
                        "candidature laissée pour complétion manuelle", blocker,
                    )
                    return False

                if self._find_in_frame(frame, self.submit_button_selectors) is not None:
                    return True

                advance = self._find_advance_button(frame)
                if advance is None:
                    logger.info("indeed: ni bouton Next/Review ni Submit trouvé — abandon")
                    return False
                advance.click(timeout=8000)
                _pause()

            logger.info("indeed: modale Indeed Apply trop longue (>%d étapes) — abandon", MAX_STEPS)
            return False
        except Exception as exc:
            logger.warning("indeed: progression Indeed Apply interrompue (%s)", exc)
            return False

    def find_submit_button(self, page: Any) -> Optional[Any]:
        frame = self._frame(page)
        if frame is None:
            return super().find_submit_button(page)
        button = self._find_in_frame(frame, self.submit_button_selectors)
        return button if button is not None else super().find_submit_button(page)

    def success_proof(self, page: Any) -> Optional[Dict[str, Any]]:
        try:
            body = page.inner_text("body") or ""
        except Exception:
            body = ""
        for text in self.SUCCESS_TEXTS:
            if text in body.lower():
                return {"type": "text", "value": text}
        return None

    # -- Étapes internes de la modale (iframe) --------------------------------

    def _frame(self, page: Any):
        try:
            if page.locator(IFRAME_SELECTOR).count() == 0:
                return None
            return page.frame_locator(IFRAME_SELECTOR)
        except Exception:
            return None

    def _find_in_frame(self, frame: Any, selectors: Any) -> Optional[Any]:
        for selector in selectors:
            try:
                button = _first_visible(frame.locator(selector))
                if button is not None:
                    return button
            except Exception:
                continue
        return None

    def _fill_known_fields(self, frame: Any, body: str) -> None:
        if self._phone:
            for selector in (
                "input[id*='phone' i]", "input[name*='phone' i]", "input[type='tel']",
            ):
                try:
                    field = _first_visible(frame.locator(selector))
                    if field is not None and not (field.input_value() or "").strip():
                        field.fill(self._phone)
                        _pause()
                        break
                except Exception:
                    continue

        if self._cv_path and self._cv_path.is_file():
            try:
                upload = _first_visible(frame.locator("input[type='file']"))
                if upload is not None:
                    upload.set_input_files(str(self._cv_path))
                    _pause()
            except Exception:
                pass

        if body.strip():
            for selector in self.message_selectors:
                try:
                    field = _first_visible(frame.locator(selector))
                    if field is not None and not (field.input_value() or "").strip():
                        field.fill(body)
                        _pause()
                        break
                except Exception:
                    continue

    def _unanswerable_field(self, frame: Any) -> Optional[str]:
        try:
            required = frame.locator("[required], [aria-required='true']")
            count = required.count()
        except Exception:
            return None

        for i in range(count):
            field = required.nth(i)
            try:
                if not field.is_visible():
                    continue
                field_type = (field.get_attribute("type") or "").lower()
                name = (field.get_attribute("name") or field.get_attribute("id") or "").lower()
                if field_type == "file" or any(marker in name for marker in _KNOWN_FIELD_MARKERS):
                    continue
                if field_type in ("radio", "checkbox"):
                    if not field.is_checked():
                        return name or "choix requis"
                    continue
                value = (field.input_value() or "").strip()
                if not value:
                    return name or "champ requis"
            except Exception:
                continue
        return None

    def _find_advance_button(self, frame: Any) -> Optional[Any]:
        try:
            buttons = frame.locator("button")
            count = buttons.count()
        except Exception:
            return None
        for i in range(count):
            btn = buttons.nth(i)
            try:
                if not btn.is_visible():
                    continue
                text = (btn.inner_text() or "").strip()
                if matches_hint(text, NEXT_HINTS) or matches_hint(text, REVIEW_HINTS):
                    return btn
            except Exception:
                continue
        return None
