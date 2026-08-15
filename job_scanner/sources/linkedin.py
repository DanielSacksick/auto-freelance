"""
linkedin.py — Source LinkedIn Jobs, via Playwright et une session authentifiée.

LinkedIn Jobs est la plateforme la plus verrouillée du lot : pages rendues en
JavaScript, `robots.txt` qui interdit `/jobs/*`, requêtes HTTP nues renvoyées
vers une page de login ou un CAPTCHA. Il n'existe donc qu'un chemin fiable :
ouvrir linkedin.com dans un vrai navigateur, avec une session authentifiée
existante (cookies exportés, voir `job_scanner/submission/sessions.py`), et
lire le DOM rendu — exactement la même approche que la soumission
(`job_scanner/submission/playwright.py`), appliquée ici à la lecture plutôt
qu'à l'envoi.

Sans session exportée pour "linkedin" (`~/.auto-freelance/sessions/linkedin.json`),
cette source ne scanne rien : elle le journalise et rend une liste vide plutôt
que de tenter un accès non authentifié voué à un mur de connexion.

Rythme et discrétion : pause aléatoire entre chaque page et chaque fiche
ouverte (même logique que la soumission — c'est le même compte qui est en
jeu), user-agent desktop honnête, viewport réaliste. Détection de mur de
connexion (session expirée) et de CAPTCHA : le scan s'arrête proprement plutôt
que d'insister.

Les sélecteurs DOM ci-dessous reflètent la structure observée sur LinkedIn
Jobs ; comme toute plateforme non documentée, ils peuvent dériver dans le
temps et méritent d'être revalidés après un premier run réel.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from job_scanner.models import RawOffer
from job_scanner.sources.base import Fetcher, OfferSource, SearchCriteria
from job_scanner.submission import sessions

logger = logging.getLogger(__name__)

BASE_URL = "https://www.linkedin.com"
SEARCH_PATH = "/jobs/search/"

DEFAULT_QUERY = "(freelance OR contract) (\"AI agent\" OR LLM OR Claude OR RAG OR MCP)"

# Pause humaine entre deux pages de résultats — même logique que
# job_scanner/submission/playwright.py : on lit un compte authentifié, pas
# une API publique.
MIN_PAUSE, MAX_PAUSE = 1.5, 3.5

# Pause plus courte entre deux clics sur des fiches, dans la même page.
MIN_CARD_PAUSE, MAX_CARD_PAUSE = 0.6, 1.6

# Nombre de fiches dont on va chercher la description complète par page —
# ouvrir chaque fiche a un coût en temps et en risque de détection ; on le
# borne plutôt que de le faire pour toutes les offres d'une page.
MAX_DESCRIPTIONS_PER_PAGE = 10

# Une page pleine compte ce nombre de cartes ; une page plus courte est la
# dernière (mêmes conventions que freework.py / freelancermap.py).
RESULTS_PER_PAGE = 25

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1360, "height": 900}

CARD_SELECTOR = "li[data-occludable-job-id]"
TITLE_SELECTORS = (
    "a.job-card-list__title", "a.job-card-container__link",
    ".job-card-list__title", "[class*='job-card-list__title']",
)
COMPANY_SELECTORS = (
    ".job-card-container__primary-description",
    ".job-card-container__company-name",
    "[class*='job-card-container__company-name']",
)
LOCATION_SELECTORS = (
    ".job-card-container__metadata-item",
    "[class*='job-card-container__metadata']",
)
DETAIL_SELECTORS = (
    ".jobs-search__job-details--container",
    ".jobs-details__main-content",
    "#job-details",
)

LOGIN_URL_MARKERS = ("/login", "/authwall", "/checkpoint", "linkedin.com/uas/")

_WS_RE = re.compile(r"\s+")


def _pause(min_s: float = MIN_PAUSE, max_s: float = MAX_PAUSE) -> None:
    """Pause courte et aléatoire : rythme humain, pas de rafale de requêtes."""
    time.sleep(random.uniform(min_s, max_s))


def _clean(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    cleaned = _WS_RE.sub(" ", text).strip()
    return cleaned or None


class LinkedInJobsSource(OfferSource):
    """Lit les offres freelance publiées sur LinkedIn Jobs, via Playwright."""

    key = "linkedin"

    def __init__(
        self,
        fetcher: Optional[Fetcher] = None,
        session_dir: Optional[Any] = None,
        headless: bool = True,
        base_url: str = BASE_URL,
    ):
        """
        Args:
            fetcher: ignoré — cette source pilote son propre navigateur plutôt
                que de passer par le `Fetcher` HTTP commun (session
                authentifiée requise, cf. docstring du module).
            session_dir: répertoire des sessions exportées (défaut
                ~/.auto-freelance/sessions, voir `submission/sessions.py`).
            headless: navigateur invisible (True par défaut — cron/tests).
            base_url: URL de base LinkedIn.
        """
        self._fetcher = fetcher
        self._session_dir = session_dir
        self._headless = headless
        self._base_url = base_url.rstrip("/")

    # -- API publique ------------------------------------------------------

    def fetch(self, criteria: SearchCriteria) -> List[RawOffer]:
        if not sessions.has_session(self.key, self._session_dir):
            logger.info(
                "linkedin: aucune session exportée — scan ignoré. Exporte-la : "
                "python -m job_scanner.submission.playwright --export linkedin"
            )
            return []

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("linkedin: Playwright n'est pas installé dans ce venv")
            return []

        offers: List[RawOffer] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self._headless)
            try:
                state = sessions.load_storage_state(self.key, self._session_dir)
                context = browser.new_context(
                    storage_state=state,
                    user_agent=USER_AGENT,
                    viewport=VIEWPORT,
                    locale=criteria.extra.get("locale", "fr-FR"),
                )
                page = context.new_page()
                offers = self._scan(page, criteria)
            except Exception as exc:
                logger.warning("linkedin: scan interrompu (%s)", exc)
            finally:
                browser.close()

        logger.info("linkedin: %d offre(s) trouvée(s)", len(offers))
        return offers

    # -- Parcours ------------------------------------------------------------

    def _scan(self, page: Any, criteria: SearchCriteria) -> List[RawOffer]:
        offers: List[RawOffer] = []
        seen: set = set()

        for page_index in range(max(1, criteria.max_pages)):
            url = self._build_search_url(criteria, page_index)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _pause()

            if self._on_login_wall(page):
                logger.warning("linkedin: session expirée ou invalide — scan interrompu")
                break
            if self._captcha_present(page):
                logger.warning(
                    "linkedin: CAPTCHA détecté — scan interrompu pour ne pas risquer le compte"
                )
                break

            cards = self._read_cards(page)
            if not cards:
                break

            described = 0
            for card in cards:
                if card["external_id"] in seen:
                    continue
                seen.add(card["external_id"])

                if described < MAX_DESCRIPTIONS_PER_PAGE:
                    card["description"] = self._read_description(page, card["locator"])
                    described += 1
                    _pause(MIN_CARD_PAUSE, MAX_CARD_PAUSE)

                offer = self._to_offer(card)
                if offer is not None:
                    offers.append(offer)

            if len(cards) < RESULTS_PER_PAGE:
                break

        return offers

    def _build_search_url(self, criteria: SearchCriteria, page_index: int) -> str:
        query = criteria.query or DEFAULT_QUERY
        location = criteria.extra.get("location", "France")
        params: Dict[str, Any] = {"keywords": query, "location": location}
        if page_index > 0:
            params["start"] = page_index * RESULTS_PER_PAGE
        return f"{self._base_url}{SEARCH_PATH}?{urlencode(params)}"

    # -- Détections de mur ----------------------------------------------------

    def _on_login_wall(self, page: Any) -> bool:
        """Session expirée ou absente : LinkedIn a renvoyé vers une connexion."""
        try:
            url = (page.url or "").lower()
        except Exception:
            return False
        if any(marker in url for marker in LOGIN_URL_MARKERS):
            return True
        try:
            return page.locator("input[type='password']").count() > 0
        except Exception:
            return False

    def _captcha_present(self, page: Any) -> bool:
        from job_scanner.submission.forms.base import CAPTCHA_HINTS, matches_hint

        for frame_selector in (
            "iframe[src*='captcha']", "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
        ):
            try:
                if page.locator(frame_selector).count() > 0:
                    return True
            except Exception:
                continue
        try:
            body = (page.inner_text("body") or "")[:4000]
        except Exception:
            return False
        return matches_hint(body, CAPTCHA_HINTS)

    # -- Parsing ---------------------------------------------------------------

    def _read_cards(self, page: Any) -> List[Dict[str, Any]]:
        cards: List[Dict[str, Any]] = []
        locator = page.locator(CARD_SELECTOR)
        try:
            count = locator.count()
        except Exception:
            return cards

        for i in range(count):
            info = self._extract_card(locator.nth(i))
            if info is not None:
                cards.append(info)
        return cards

    def _extract_card(self, card: Any) -> Optional[Dict[str, Any]]:
        try:
            job_id = card.get_attribute("data-occludable-job-id")
            if not job_id:
                return None

            title = self._first_text(card, TITLE_SELECTORS)
            if not title:
                return None

            return {
                "locator": card,
                "external_id": job_id,
                "title": title[:300],
                "company": (self._first_text(card, COMPANY_SELECTORS) or "")[:200] or None,
                "location": (self._first_text(card, LOCATION_SELECTORS) or "")[:100] or None,
                "url": f"{self._base_url}/jobs/view/{job_id}/",
            }
        except Exception as exc:
            logger.debug("linkedin: fiche ignorée (%s)", exc)
            return None

    @staticmethod
    def _first_text(card: Any, selectors: tuple) -> Optional[str]:
        for selector in selectors:
            try:
                element = card.locator(selector).first
                if element.count():
                    text = _clean(element.inner_text())
                    if text:
                        return text
            except Exception:
                continue
        return None

    def _read_description(self, page: Any, card: Any) -> Optional[str]:
        """Ouvre une fiche (clic dans la liste, pas de navigation) et lit le
        panneau de détail. Un échec n'est pas fatal : l'offre reste sans
        description plutôt que d'interrompre le scan."""
        try:
            card.click(timeout=8000)
            page.wait_for_timeout(1200)
            for selector in DETAIL_SELECTORS:
                panel = page.locator(selector).first
                if panel.count():
                    text = _clean(panel.inner_text())
                    if text:
                        return text[:2000]
        except Exception as exc:
            logger.debug("linkedin: description illisible (%s)", exc)
        return None

    # -- Traduction vers le modèle pivot ---------------------------------------

    def _to_offer(self, card: Dict[str, Any]) -> Optional[RawOffer]:
        try:
            return RawOffer(
                source=self.key,
                external_id=card["external_id"],
                url=card["url"],
                title=card["title"],
                company=card.get("company"),
                description=card.get("description"),
                currency="EUR",
                location=card.get("location"),
                contract_type="contractor",
                raw={"scanned_via": "playwright"},
            )
        except Exception as exc:
            logger.warning("linkedin: offre %s ignorée (%s)", card.get("external_id"), exc)
            return None
