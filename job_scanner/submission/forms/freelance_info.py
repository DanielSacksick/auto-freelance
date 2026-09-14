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

**Changement DOM constaté (vérifié en conditions réelles, 01/09/2026)** : le
CTA « Postuler » n'est plus un `<a href="/candidature-…">` mais un
`<span class="btn-postuler" data-obf="<base64 du chemin>">` — le chemin de
candidature est encodé en base64 dans `data-obf`, probablement pour gêner le
scraping naïf. Cliquer le span déclenche un décodage JS côté client, pas
toujours fiable en automatisation headless ; le mapper décode donc lui-même
`data-obf` et navigue directement vers le chemin obtenu — plus robuste qu'un
clic, et cohérent avec le fait que `goto_application` sait déjà ouvrir un
chemin `/candidature-…` directement.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse

from job_scanner.submission.forms.base import FormMapper
from job_scanner.submission.forms import register

logger = logging.getLogger(__name__)

# Chemin de candidature : /candidature-<slug>-<reference>
_CANDIDATURE_RE = re.compile(r"/candidature-[^/]+$")


@register
class FreelanceInfoForm(FormMapper):
    """Freelance-Informatique — HTML classique, formulaire à /candidature-<slug>."""

    key = "freelance-informatique"

    # Sur la fiche mission, le CTA « Postuler » est soit un lien direct
    # (a.btn…, ancien DOM), soit un span dont le chemin est encodé en base64
    # dans data-obf (DOM constaté depuis le 01/09/2026, voir docstring module).
    apply_button_selectors = (
        "a[href*='/candidature-']",
        "span.btn-postuler[data-obf]",
        "[data-obf]",
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
                if not locator.count():
                    continue
                target_path = None
                href = locator.get_attribute("href")
                if href and "/candidature-" in href:
                    target_path = href
                else:
                    obf = locator.get_attribute("data-obf")
                    if obf:
                        target_path = _decode_obf(obf)
                if target_path and "/candidature-" in target_path:
                    # Relatif à l'origine courante (jamais un domaine en dur :
                    # la fiche et le formulaire vivent sur le même site).
                    target = urljoin(page.url, target_path)
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


def _decode_obf(value: str) -> Optional[str]:
    """Décode un attribut `data-obf` (chemin encodé en base64). Rend None sur
    une valeur invalide plutôt que de lever — le DOM d'un site tiers peut
    changer d'encodage sans prévenir, ce n'est jamais une raison de planter."""
    try:
        return base64.b64decode(value).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
