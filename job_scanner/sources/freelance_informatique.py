"""
freelance_informatique.py — Source Freelance-Informatique, le gisement ESN français.

Le site publie ses missions en clair et les expose même à Google for Jobs
(`sitemap_missions_google_for_jobs.xml`, données JSON-LD schema.org). Le
`robots.txt` autorise explicitement les pages de missions. Une requête HTTP par
recherche suffit : **aucune automatisation de navigateur, aucun risque de compte**.

Deux choix de conception méritent une explication.

**On lit la page de résultats, pas les fiches.** La carte de résultat porte déjà
tout ce que le modèle pivot demande : titre, description complète, date de
publication, lieu, date de démarrage, durée. Ouvrir chaque annonce coûterait
cinquante requêtes par page pour rien.

**Le TJM n'existe pas ici.** Il est masqué derrière un bouton « Voir le tarif ».
`daily_rate_*` reste donc vide : le scoring traite déjà l'absence comme neutre,
là où inventer une valeur fausserait le classement.

⚠️ Piège de pagination : au-delà de la dernière page de résultats, le site
renvoie la liste **non filtrée** des missions. La consommer remplirait la base
de COBOL et de SAP. Le compteur « Offres X à Y sur N » sert donc de garde-fou :
sans lui, on s'arrête.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from job_scanner.models import RawOffer
from job_scanner.sources.base import Fetcher, OfferSource, SearchCriteria

logger = logging.getLogger(__name__)

BASE_URL = "https://www.freelance-informatique.fr"
SEARCH_PATH = "/offres-freelance"

# Termes cherchés par défaut, dans le champ « compétences » du site. Ils se
# recouvrent volontairement : le moteur est strict, et une mission d'agents IA
# peut être taguée « IA » sans jamais mentionner « LLM ».
DEFAULT_TERMS: Tuple[str, ...] = (
    "IA",
    "intelligence artificielle",
    "machine learning",
    "LLM",
    "RAG",
)

# « Offres 51 à 71 sur 71 résultats » — la preuve qu'on est bien dans une
# recherche filtrée, et de quoi savoir quand s'arrêter.
_COUNTER_RE = re.compile(
    r"Offres\s+(\d+)\s+à\s+(\d+)\s+sur\s+(\d+)\s+résultats", re.I
)

# Référence de la mission en fin de slug : 260729G003.
_REFERENCE_RE = re.compile(r"-(\d{6}[A-Z]\d{3})$")

_SHORT_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})")
_DURATION_RE = re.compile(r"(\d+)\s*(jours?|mois|ans?|an)\b", re.I)

_WORKING_DAYS_PER_MONTH = 20


@dataclass(frozen=True)
class FreelanceInfoConfig:
    """Termes cherchés et volume par page."""

    terms: Tuple[str, ...] = DEFAULT_TERMS
    page_size: int = 50

    @classmethod
    def from_env(cls) -> "FreelanceInfoConfig":
        """Lit `FREELANCE_INFO_TERMS`, une liste séparée par des virgules."""
        defaults = cls()
        raw = os.environ.get("FREELANCE_INFO_TERMS")
        if not raw:
            return defaults
        terms = tuple(term.strip() for term in raw.split(",") if term.strip())
        return replace(defaults, terms=terms or defaults.terms)


class FreelanceInformatiqueSource(OfferSource):
    """Lit les missions freelance publiées sur Freelance-Informatique."""

    key = "freelance-informatique"

    def __init__(
        self,
        fetcher: Fetcher,
        config: Optional[FreelanceInfoConfig] = None,
        base_url: str = BASE_URL,
    ):
        self._fetch_page = fetcher
        self._config = config or FreelanceInfoConfig()
        self._base_url = base_url.rstrip("/")

    # -- API publique ----------------------------------------------------

    def fetch(self, criteria: SearchCriteria) -> List[RawOffer]:
        offers: List[RawOffer] = []
        seen: set = set()

        for term in self._config.terms:
            for offer in self._search(term, criteria):
                if offer.external_id in seen:
                    continue
                seen.add(offer.external_id)
                offers.append(offer)

        return offers

    # -- Récupération ----------------------------------------------------

    def _search(self, term: str, criteria: SearchCriteria) -> List[RawOffer]:
        """Parcourt les pages d'un terme ; un terme en échec n'arrête pas les autres."""
        found: List[RawOffer] = []

        for page in range(1, max(1, criteria.max_pages) + 1):
            html = self._read(self._build_url(term, page))
            if html is None:
                break

            soup = BeautifulSoup(html, "lxml")
            counter = _read_counter(soup)
            if counter is None:
                # Plus de recherche filtrée : le site sert la liste générale.
                if page > 1:
                    logger.debug("freelance-informatique: fin des résultats pour « %s »", term)
                else:
                    logger.info("freelance-informatique: aucun résultat pour « %s »", term)
                break

            found.extend(self._read_cards(soup))

            _, last, total = counter
            if last >= total:
                break

        return found

    def _read(self, url: str) -> Optional[str]:
        try:
            return self._fetch_page(url)
        except Exception as exc:  # réseau, timeout, statut HTTP
            logger.warning("freelance-informatique: page inaccessible %s (%s)", url, exc)
            return None

    def _build_url(self, term: str, page: int) -> str:
        params: Dict[str, Any] = {"competences": term}
        if page > 1:
            params["page"] = page
        return f"{self._base_url}{SEARCH_PATH}?{urlencode(params)}"

    def _read_cards(self, soup: BeautifulSoup) -> List[RawOffer]:
        offers: List[RawOffer] = []
        for card in soup.select(".job-card-line"):
            offer = self._to_offer(card)
            if offer is not None:
                offers.append(offer)
        return offers

    # -- Traduction vers le modèle pivot ---------------------------------

    def _to_offer(self, card: Any) -> Optional[RawOffer]:
        """Traduit une carte de résultat ; rend None si elle est inexploitable."""
        link = card.select_one("h2.job-title a") or card.select_one("a[href^='/mission-']")
        href = link.get("href") if link else None
        title = link.get_text(strip=True) if link else None
        if not href or not title:
            return None

        meta = _read_meta(card)
        paragraph = card.select_one("p")

        try:
            return RawOffer(
                source=self.key,
                external_id=_reference(href),
                url=f"{self._base_url}{href}",
                title=title,
                company=None,  # le site intermédie : le client final n'est pas nommé
                description=paragraph.get_text(" ", strip=True) if paragraph else None,
                published_at=parse_short_date(meta.get("published")),
                # Le TJM est masqué derrière « Voir le tarif » : on n'invente rien.
                daily_rate_min=None,
                daily_rate_max=None,
                currency="EUR",
                duration_months=parse_duration_months(meta.get("duration")),
                remote_mode=None,  # non publié sur la carte
                location=meta.get("place"),
                contract_type="contractor",
                raw={
                    "href": href,
                    "duration_label": meta.get("duration"),
                    "start_date": meta.get("start"),
                    "published_label": meta.get("published"),
                    "place_label": meta.get("place"),
                },
            )
        except Exception as exc:  # structure inattendue : on saute cette carte
            logger.warning("freelance-informatique: carte ignorée (%s)", exc)
            return None


# -- Utilitaires de parsing ----------------------------------------------


def _read_counter(soup: BeautifulSoup) -> Optional[Tuple[int, int, int]]:
    """Rend (premier, dernier, total) de « Offres X à Y sur N résultats »."""
    match = _COUNTER_RE.search(soup.get_text(" ", strip=True))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _read_meta(card: Any) -> Dict[str, str]:
    """
    Lit les métadonnées d'une carte par la classe de leur icône.

    Se repérer sur `icon-clock` ou `icon-map` plutôt que sur l'ordre des `<li>`
    évite qu'un jour sans date de démarrage décale toutes les valeurs.
    """
    by_icon = {
        "icon-clock": "published",
        "icon-map": "place",
        "icon-calendar": "start",
        "icon-time": "duration",
    }
    meta: Dict[str, str] = {}

    for item in card.select("ul li"):
        icon = item.select_one("i")
        classes = icon.get("class") or [] if icon else []
        field = next((by_icon[c] for c in classes if c in by_icon), None)
        if not field:
            continue
        value = item.get_text(" ", strip=True)
        if value:
            meta[field] = value

    return meta


def _reference(href: str) -> str:
    """
    Identifiant stable de la mission.

    Le site termine ses slugs par une référence interne (`260729G003`), qui ne
    bouge pas si le titre est réécrit. À défaut, le slug entier fait l'affaire.
    """
    match = _REFERENCE_RE.search(href)
    if match:
        return match.group(1)
    return href.strip("/").split("/")[-1]


def parse_short_date(label: Any, today: Optional[datetime] = None) -> Optional[datetime]:
    """
    Lit « Publiée le 29/07 », que le site affiche sans année.

    Une date qui tomberait dans le futur ne peut être que celle de l'an dernier :
    c'est le seul cas où l'année implicite est ambiguë, et il se tranche seul.
    """
    if not isinstance(label, str):
        return None
    match = _SHORT_DATE_RE.search(label)
    if not match:
        return None

    reference = today or datetime.now()
    day, month = int(match.group(1)), int(match.group(2))
    try:
        candidate = datetime(reference.year, month, day)
    except ValueError:
        return None

    if candidate > reference:
        try:
            return datetime(reference.year - 1, month, day)
        except ValueError:
            return None
    return candidate


def parse_duration_months(label: Any) -> Optional[int]:
    """Ramène « 3 mois », « 1 an » ou « 10 jours ouvrés » à un nombre de mois."""
    if not isinstance(label, str):
        return None
    match = _DURATION_RE.search(label)
    if not match:
        return None

    value, unit = int(match.group(1)), match.group(2).lower()
    if value <= 0:
        return None

    if unit.startswith("mois"):
        return value
    if unit.startswith("an"):
        return value * 12
    # Le site compte en jours ouvrés : un mois de mission en vaut une vingtaine.
    return max(1, round(value / _WORKING_DAYS_PER_MONTH))
