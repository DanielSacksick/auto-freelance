"""
forms/wttj.py — Formulaire de candidature Welcome to the Jungle (WTTJ).

Constaté en conditions réelles (01/09/2026), sans session :

- Sur la fiche mission, le CTA « Postuler » existe sous deux formes : des
  `<a href="/fr/authenticate/signin">` (liens de secours, toujours présents)
  et des `<button>Postuler</button>` sans `href`, pilotés en JavaScript.
- Sans session, cliquer le bouton ne fait rien d'observable — comme
  FreeWork, la candidature est **strictement réservée aux connectés** ; WTTJ
  n'affiche même pas de modale de connexion au clic, il ne se passe rien.

Le formulaire de candidature lui-même (une fois connecté) n'a pas pu être
observé — impossible de dépasser le mur de connexion sans session exportée.
Le mapper ne peut donc déclarer de sélecteurs de message/envoi fiables issus
d'une observation directe : il retombe entièrement sur la recherche par
intention du moteur générique (`FormMapper.fill_message` /
`find_submit_button`, voir `forms/base.py`), qui cherche un textarea et un
bouton d'envoi par motifs textuels plutôt que par classe CSS exacte.

**Sûreté avant efficacité** : tant que ce mapper n'a pas été validé de bout
en bout avec une session réelle (voir `SESSION_REQUIREMENTS.md`), toute
tentative de soumission automatique s'arrêtera proprement au mur de
connexion (session_required) ou faute de champ trouvé (error) — jamais de
candidature à moitié remplie ou d'état incertain.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from job_scanner.submission.forms.base import FormMapper
from job_scanner.submission.forms import register

logger = logging.getLogger(__name__)


@register
class WTTJForm(FormMapper):
    """Welcome to the Jungle — bouton Postuler JavaScript, formulaire derrière session."""

    key = "wttj"

    apply_button_selectors = (
        "button:has-text('Postuler')",
        "a:has-text('Postuler')",
    )
    # Non observés au-delà du mur de connexion — repli sur la recherche par
    # intention générique du moteur (voir docstring module).
    message_selectors = (
        "textarea[name*='message' i], textarea[name*='motivation' i], "
        "textarea[placeholder*='message' i], textarea[placeholder*='motivation' i], "
        "textarea[placeholder*='lettre' i]",
        "form textarea",
    )
    submit_button_selectors = (
        "button[type='submit']:has-text('Envoyer')",
        "button[type='submit']:has-text('Postuler')",
        "button[type='submit']",
    )

    # Confirmation visible après envoi réussi (formulation la plus probable,
    # cohérente avec le ton du site — à ajuster après premier test réel).
    SUCCESS_TEXTS = (
        "candidature envoyée", "candidature bien envoyée", "candidature transmise",
        "votre candidature a été envoyée", "merci pour votre candidature",
        "application sent", "application submitted",
    )

    def prepare(self, page: Any) -> None:
        """Ferme le banner de consentement Axeptio (WTTJ)."""
        for selector in (
            "#axeptio_btn_acceptAll",
            "button:has-text('Tout accepter')",
            "button:has-text('Accepter')",
        ):
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    locator.click(timeout=3000)
                    logger.debug("wttj: banner cookies fermé")
                    return
            except Exception:
                continue

    def goto_application(self, page: Any, url: str) -> None:
        """Ouvre la fiche mission ; le bouton Postuler y est."""
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

    def success_proof(self, page: Any) -> Optional[Dict[str, Any]]:
        try:
            body = page.inner_text("body") or ""
        except Exception:
            body = ""
        for text in self.SUCCESS_TEXTS:
            if text in body.lower():
                return {"type": "text", "value": text}
        return None
