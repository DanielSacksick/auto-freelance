"""
forms/base.py — Contrat d'un formulaire de candidature.

Chaque plateforme (FreeWork, Freelance-Informatique, freelancermap…) expose un
formulaire différent : React, Nuxt, HTML classique. Le moteur Playwright
(`job_scanner.submission.playwright`) pilote le navigateur ; un `FormMapper`
lui dit *où* sont les choses — bouton « postuler », champ message, bouton
d'envoi, preuve de succès.

Deux stratégies de remplissage, complémentaires :

- **Sélecteurs déclarés** : le mapper connaît les sélecteurs stables (ex.
  `button.btn--secondary:has-text("Postuler")`). À utiliser quand le DOM est
  connu.
- **Recherche par intention** : à défaut, on cherche le champ qui ressemble à
  un message (textarea) et le bouton qui ressemble à un envoi, par des motifs
  textuels et structurels. Moins précis, mais robuste aux changements de
  classes CSS.

Le mapper ne soumet JAMAIS lui-même : il prépare le terrain, et le moteur
décide. La confirmation d'envoi revient au moteur, qui vérifie la preuve de
succès rendue par `success_proof()` avant de marquer `applied`.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Motifs de libellés qui désignent un champ de message de candidature.
MESSAGE_HINTS = (
    "message", "présentation", "presentation", "motivation", "lettre",
    "cover", "commentaire", "comment", "texte", "text", "profil", "cv",
)

# Motifs de libellés de boutons d'envoi.
SUBMIT_HINTS = (
    "postuler", "envoyer", "send", "submit", "apply", "bewerben",
    "valider", "confirm", "candidater", "soumettre",
)

# Motifs qui signalent une page de connexion (le formulaire est derrière).
LOGIN_WALL_HINTS = (
    "se connecter", "log in", "sign in", "connexion", "login",
    "mot de passe", "password", "email", "courriel",
)

# Motifs qui signalent un CAPTCHA — FreeWork est le plus susceptible d'en avoir.
CAPTCHA_HINTS = (
    "captcha", "recaptcha", "hcaptcha", "are you human", "vérification de sécurité",
    "security check", "challenge", "robot",
)


def _norm(value: Any) -> str:
    """Normalise un texte pour comparaison : minuscules, sans accents ni ponctuation."""
    if value is None:
        return ""
    text = str(value).lower()
    for accented, plain in (
        ("é", "e"), ("è", "e"), ("ê", "e"), ("ë", "e"),
        ("à", "a"), ("â", "a"), ("ä", "a"),
        ("î", "i"), ("ï", "i"),
        ("ô", "o"), ("ö", "o"),
        ("ù", "u"), ("û", "u"), ("ü", "u"),
        ("ç", "c"), ("œ", "oe"), ("æ", "ae"),
    ):
        text = text.replace(accented, plain)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def matches_hint(text: Any, hints: Iterable[str]) -> bool:
    """Un libellé normalisé contient-il l'un des motifs donnés ?"""
    norm = _norm(text)
    if not norm:
        return False
    return any(f" {hint} " in f" {norm} " or norm.startswith(f"{hint} ")
               or norm.endswith(f" {hint}") or norm == hint
               for hint in hints)


class FormMapper(ABC):
    """
    Interface commune : le moteur appelle `prepare`, `fill`, puis vérifie.

    Le cycle complet, orchestré par le moteur :
      1. `goto_application(page, url)` — ouvre le bon endroit (fiche mission
         ou formulaire direct) ;
      2. `find_apply_button(page)` — rend le bouton « postuler », ou None si
         déjà sur le formulaire ;
      3. `fill_message(page, body)` — remplit le champ message ;
      4. `find_submit_button(page)` — rend le bouton d'envoi ;
      5. `success_proof(page)` — preuve observable qu'une candidature est
         partie (URL, texte, attribut).
    """

    #: Clé source (freework, freelance-informatique, freelancermap…).
    key: str = ""

    #: Sélecteurs CSS du bouton « postuler », testés dans l'ordre.
    apply_button_selectors: Tuple[str, ...] = ()

    #: Sélecteurs CSS du bouton d'envoi final, testés dans l'ordre.
    submit_button_selectors: Tuple[str, ...] = ()

    #: Sélecteurs CSS des champs message, testés dans l'ordre.
    message_selectors: Tuple[str, ...] = ()

    #: Regex d'URL qui prouve qu'on est bien sur le formulaire de candidature.
    application_url_pattern: Optional[re.Pattern] = None

    # -- Cycle de vie ------------------------------------------------------

    @abstractmethod
    def prepare(self, page: Any) -> None:
        """Prépare la page : ferme modales, banners cookies, etc."""

    @abstractmethod
    def goto_application(self, page: Any, url: str) -> None:
        """Navigue vers la fiche ou le formulaire de candidature."""

    def find_apply_button(self, page: Any) -> Optional[Any]:
        """Rend le premier bouton « postuler » visible, ou None."""
        for selector in self.apply_button_selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() and locator.is_visible():
                    return locator
            except Exception:
                continue
        # Repli : recherche textuelle large, sans dépendre d'une classe CSS.
        return _find_visible_by_text(page, "button", MESSAGE_HINTS, SUBMIT_HINTS)

    def fill_message(self, page: Any, body: str) -> bool:
        """Remplit le champ message ; rend True si un champ a été rempli.
        Fallback JS pour les SPA où le textarea est dans le DOM mais pas visible."""
        for selector in self.message_selectors:
            locator = page.locator(selector).first
            try:
                exists = page.evaluate(
                    f"(sel) => document.querySelector(sel) !== null", selector
                )
                if exists:
                    # JS fill pour les SPA
                    page.evaluate(f"""
                        (sel, val) => {{
                            const el = document.querySelector(sel);
                            if (el) {{ el.value = val; el.dispatchEvent(new Event('input', {{bubbles: true}})); }}
                        }}
                    """, selector, body)
                    return True
            except Exception:
                continue
        # Repli : la première textarea visible, si elle ressemble à un message.
        textarea = _find_visible_by_text(page, "textarea", None, MESSAGE_HINTS)
        if textarea is not None:
            textarea.fill(body)
            return True
        return False

    def find_submit_button(self, page: Any) -> Optional[Any]:
        """Rend le premier bouton d'envoi visible, ou None.
        Fallback : vérifie l'existence dans le DOM via JS pour les SPA."""
        for selector in self.submit_button_selectors:
            locator = page.locator(selector).first
            try:
                exists = page.evaluate(
                    f"(sel) => document.querySelector(sel) !== null", selector
                )
                if exists:
                    return locator
            except Exception:
                continue
        return _find_visible_by_text(page, "button", SUBMIT_HINTS, SUBMIT_HINTS)

    @abstractmethod
    def success_proof(self, page: Any) -> Optional[Dict[str, Any]]:
        """
        Preuve observable qu'une candidature a été envoyée, ou None.

        Retourne un dict descriptif (ex. {"type": "url", "value": "…"},
        {"type": "text", "value": "…"}, {"type": "hidden"}) qui servira de
        confirmation au moteur avant de marquer `applied`.
        """


# -- Recherche générique par intention ------------------------------------


def _find_visible_by_text(
    page: Any, tag: str, include_hints: Optional[Iterable[str]],
    exclude_hints: Optional[Iterable[str]],
) -> Optional[Any]:
    """
    Cherche un élément `<tag>` visible dont le texte contient un motif.

    `include_hints=None` : n'importe quel texte convient (prend le premier
    visible). `exclude_hints` : motifs qui disqualifient (ex. un bouton
    « Se connecter » n'est pas un bouton « postuler »).
    """
    elements = page.locator(tag)
    count = elements.count()
    if count == 0:
        return None
    for i in range(count):
        locator = elements.nth(i)
        try:
            if not locator.is_visible():
                continue
            text = (locator.inner_text() or "").strip()
            if not text:
                continue
            if include_hints is not None and not matches_hint(text, include_hints):
                continue
            if exclude_hints is not None and matches_hint(text, exclude_hints):
                continue
            return locator
        except Exception:
            continue
    return None
