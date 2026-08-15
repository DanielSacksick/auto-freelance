"""
forms/linkedin.py — Formulaire de candidature LinkedIn (Easy Apply).

LinkedIn expose deux façons de postuler : un bouton « Apply » qui renvoie vers
le site du client (hors périmètre, pas automatisable — aucun `FormMapper` ne
peut le suivre en toute sécurité), et un bouton « Easy Apply » qui ouvre une
modale interne remplie en plusieurs étapes (Next → Next → … → Review → Submit
application). `apply_button_selectors` ne cible que ce second cas.

Contrairement aux trois autres plateformes, cette progression multi-étapes ne
rentre pas dans le cycle en une passe du moteur générique
(`submission/playwright.py` : postuler → remplir → envoyer). Ce mapper
l'absorbe donc dans `fill_message`, qui avance la modale étape par étape et ne
rend `True` que si elle atteint l'écran final « Submit application » — c'est
alors `find_submit_button`/`success_proof` (hérités du comportement standard
de `FormMapper`) qui terminent le cycle normalement.

Règle de sûreté : ce mapper ne complète que les champs qu'il connaît avec
certitude — téléphone (`AUTOFREELANCE_PHONE`), CV (`AUTOFREELANCE_CV_PATH`),
message à l'employeur (le texte rédigé par `drafting/`). Ces deux variables
d'environnement suivent la convention `AUTOFREELANCE_*` déjà utilisée ailleurs
dans ce dépôt (voir `.env.example`) ; on ne fait pas transiter `config.yml`
jusqu'ici, le moteur de soumission ne reçoit aujourd'hui que des primitives
(source, url, body), pas l'`AppConfig` complète.

Une question de sélection additionnelle (texte libre, choix, case à cocher
hors ces trois champs) que le mapper ne peut pas remplir sans inventer une
réponse fait abandonner proprement la candidature — jamais de réponse
fabriquée à une question de recrutement (cf. CLAUDE.md, « jamais inventer un
chiffre, un lead, un résultat »). La modale reste simplement ouverte ; comme
le bouton d'envoi n'est jamais cliqué, rien ne part.
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

# Pause entre deux actions à l'intérieur de la modale (remplissage, clic
# Next) — même logique de rythme humain que job_scanner/submission/playwright.py.
MIN_STEP_PAUSE, MAX_STEP_PAUSE = 0.8, 2.0

# Nombre d'étapes Easy Apply toléré avant d'abandonner : au-delà, mieux vaut
# une revue manuelle qu'une boucle qui s'entête sur une modale inattendue.
MAX_STEPS = 8

NEXT_HINTS = ("next", "suivant", "continue", "continuer")
REVIEW_HINTS = ("review", "verifier", "vérifier")

# Champs requis qu'on sait remplir sans deviner ; tout autre champ requis et
# vide fait abandonner (cf. docstring du module).
_KNOWN_FIELD_MARKERS = ("phone", "telephone", "téléphone", "message")


def _pause() -> None:
    time.sleep(random.uniform(MIN_STEP_PAUSE, MAX_STEP_PAUSE))


def _first_visible(locator: Any) -> Optional[Any]:
    """Le premier élément visible d'un locator, ou None. Une modale
    multi-étapes garde parfois les autres étapes montées mais masquées :
    ignorer la visibilité ferait remplir ou bloquer sur un champ d'une étape
    pas encore atteinte."""
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
class LinkedInForm(FormMapper):
    """LinkedIn — modale Easy Apply multi-étapes, formulaire derrière session."""

    key = "linkedin"

    apply_button_selectors = (
        "button.jobs-apply-button:has-text('Easy Apply')",
        "button:has-text('Easy Apply')",
        "button:has-text('Postuler facilement')",
    )
    submit_button_selectors = (
        "button[aria-label='Submit application']",
        "button:has-text('Submit application')",
        "button:has-text('Envoyer la candidature')",
    )
    message_selectors = (
        "textarea[id*='message' i]",
        "textarea[name*='message' i]",
        "form textarea",
    )

    # Confirmation visible après envoi réussi.
    SUCCESS_TEXTS = (
        "application sent", "your application was sent",
        "candidature envoyée", "votre candidature a été envoyée",
    )

    def __init__(self) -> None:
        self._phone = os.environ.get("AUTOFREELANCE_PHONE", "").strip()
        cv_path = os.environ.get("AUTOFREELANCE_CV_PATH", "").strip()
        self._cv_path = Path(cv_path).expanduser() if cv_path else None

    # -- Cycle de vie --------------------------------------------------------

    def prepare(self, page: Any) -> None:
        """Ferme les éventuels banners de cookies."""
        _close_consent_banner(page)

    def goto_application(self, page: Any, url: str) -> None:
        """Ouvre la fiche mission ; le bouton Easy Apply y est."""
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        _pause()

    def fill_message(self, page: Any, body: str) -> bool:
        """
        Absorbe toute la progression Easy Apply : (Next|Review)* jusqu'à
        l'écran « Submit application ». Rend True seulement si cet écran est
        atteint sans question à laquelle le mapper ne sait pas répondre.
        """
        try:
            if not self._modal_open(page):
                return False

            for _ in range(MAX_STEPS):
                self._fill_known_fields(page, body)

                blocker = self._unanswerable_field(page)
                if blocker is not None:
                    logger.info(
                        "linkedin: question de sélection non automatisable (%s) — "
                        "candidature laissée pour complétion manuelle", blocker,
                    )
                    return False

                if self.find_submit_button(page) is not None:
                    return True

                advance = self._find_advance_button(page)
                if advance is None:
                    logger.info("linkedin: ni bouton Next/Review ni Submit trouvé — abandon")
                    return False
                advance.click(timeout=8000)
                _pause()

            logger.info(
                "linkedin: modale Easy Apply trop longue (>%d étapes) — abandon", MAX_STEPS
            )
            return False
        except Exception as exc:
            logger.warning("linkedin: progression Easy Apply interrompue (%s)", exc)
            return False

    def find_submit_button(self, page: Any) -> Optional[Any]:
        """Comme `FormMapper.find_submit_button`, mais exige la visibilité :
        une modale multi-étapes peut garder le bouton final dans le DOM,
        masqué, avant que cette étape ne soit réellement atteinte — le
        contrôle d'existence seul (comportement par défaut) le confondrait
        avec l'étape finale."""
        for selector in self.submit_button_selectors:
            try:
                button = _first_visible(page.locator(selector))
                if button is not None:
                    return button
            except Exception:
                continue
        return super().find_submit_button(page)

    def success_proof(self, page: Any) -> Optional[Dict[str, Any]]:
        try:
            body = page.inner_text("body") or ""
        except Exception:
            body = ""
        for text in self.SUCCESS_TEXTS:
            if text in body.lower():
                return {"type": "text", "value": text}
        return None

    # -- Étapes internes de la modale -----------------------------------------

    def _modal_open(self, page: Any) -> bool:
        try:
            return page.locator(
                "div.jobs-easy-apply-modal, div[data-test-modal-id='easy-apply-modal']"
            ).first.count() > 0
        except Exception:
            return False

    def _fill_known_fields(self, page: Any, body: str) -> None:
        """Remplit uniquement les champs qu'on connaît avec certitude, et
        uniquement ceux visibles à l'étape courante — une modale multi-étapes
        peut garder les autres étapes montées dans le DOM mais masquées. Ne
        remplace jamais une valeur déjà présente (l'utilisateur peut avoir
        pré-rempli LinkedIn lui-même)."""
        if self._phone:
            for selector in (
                "input[id*='phoneNumber' i]", "input[name*='phone' i]", "input[type='tel']",
            ):
                try:
                    field = _first_visible(page.locator(selector))
                    if field is not None and not (field.input_value() or "").strip():
                        field.fill(self._phone)
                        _pause()
                        break
                except Exception:
                    continue

        if self._cv_path and self._cv_path.is_file():
            try:
                upload = _first_visible(page.locator("input[type='file']"))
                if upload is not None:
                    upload.set_input_files(str(self._cv_path))
                    _pause()
            except Exception:
                pass

        if body.strip():
            for selector in self.message_selectors:
                try:
                    field = _first_visible(page.locator(selector))
                    if field is not None and not (field.input_value() or "").strip():
                        field.fill(body)
                        _pause()
                        break
                except Exception:
                    continue

    def _unanswerable_field(self, page: Any) -> Optional[str]:
        """Rend le nom du premier champ requis, visible, vide, et hors
        téléphone/CV/message — ou None si l'étape courante ne contient rien
        de tel."""
        try:
            required = page.locator("form [required], form [aria-required='true']")
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

    def _find_advance_button(self, page: Any) -> Optional[Any]:
        """Le bouton Next ou Review de l'étape courante, ou None."""
        buttons = page.locator("button")
        try:
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


def _close_consent_banner(page: Any) -> None:
    """Ferme les banners cookies s'ils apparaissent."""
    for selector in (
        "button:has-text('Accept')", "button:has-text('Accepter')",
        "button:has-text('Tout accepter')",
    ):
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                locator.click(timeout=3000)
                logger.debug("linkedin: banner cookies fermé")
                return
        except Exception:
            continue
