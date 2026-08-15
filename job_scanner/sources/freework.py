"""
freework.py — Source Free-Work (free-work.com), le gisement principal de
missions freelance IA/data en France.

Free-Work est une application Nuxt 3 : la liste des offres n'est pas dans le
HTML rendu, mais dans le payload `__NUXT_DATA__` de la page. On le décode
(cf. `devalue.py`) et on obtient des données déjà structurées — titre, TJM,
durée, télétravail, compétences taggées.

Conséquence : **aucune automatisation de navigateur n'est nécessaire ici**, donc
aucun risque de blocage de compte. Une requête HTTP par page suffit.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus, urlencode

from job_scanner.devalue import DevalueError, extract_nuxt_payload
from job_scanner.models import RawOffer
from job_scanner.sources.base import Fetcher, OfferSource, SearchCriteria

logger = logging.getLogger(__name__)

BASE_URL = "https://www.free-work.com"
SEARCH_PATH = "/fr/tech-it/jobs"
# Les annonces vivent ailleurs que la recherche : /jobs/<slug> renvoie une 301.
OFFER_PATH = "/fr/tech-it/job-mission"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_RATE_RE = re.compile(r"\d[\d\s ]*")

# Free-Work exprime la durée en jours, mois ou années : on ramène tout en mois.
_DURATION_IN_MONTHS = {"day": 1 / 30, "week": 0.25, "month": 1, "year": 12}


def _clean_number(value: str) -> str:
    """Retire les espaces (normaux, insécables) des nombres français."""
    return value.replace(" ", "").replace("\u202f", "").replace("\u00a0", "")


def parse_daily_rate(value: Any) -> Tuple[Optional[int], Optional[int]]:
    """
    Extrait les bornes de TJM d'un libellé Free-Work.

    Gère « 500-550 € », « 600 € », « 1 200 € », ainsi que les valeurs absentes
    (chaîne vide, None, ou dict vide quand la plateforme ne publie rien).
    """
    if not isinstance(value, str) or not value.strip():
        return None, None

    numbers = [
        int(_clean_number(match))
        for match in _RATE_RE.findall(value)
        if match.strip()
    ]
    if not numbers:
        return None, None
    return min(numbers), max(numbers)


def strip_html(value: Any) -> Optional[str]:
    """Réduit une description HTML à du texte lisible par un LLM."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = html_module.unescape(_TAG_RE.sub(" ", value))
    return _WS_RE.sub(" ", text).strip() or None


class FreeWorkSource(OfferSource):
    """Lit les missions freelance publiées sur Free-Work."""

    key = "freework"

    def __init__(self, fetcher: Fetcher, base_url: str = BASE_URL):
        self._fetch_page = fetcher
        self._base_url = base_url.rstrip("/")

    # -- API publique ----------------------------------------------------

    def fetch(self, criteria: SearchCriteria) -> List[RawOffer]:
        offers: List[RawOffer] = []
        seen: set = set()

        for page in range(1, max(1, criteria.max_pages) + 1):
            for job in self._read_page(self._build_url(criteria, page)):
                offer = self._to_offer(job)
                if offer is None or offer.external_id in seen:
                    continue
                if self._below_rate_floor(offer, criteria.min_daily_rate):
                    continue
                seen.add(offer.external_id)
                offers.append(offer)

        return offers

    # -- Récupération ----------------------------------------------------

    def _build_url(self, criteria: SearchCriteria, page: int) -> str:
        params = {"query": criteria.query, "page": page}
        if criteria.contract_types:
            params["contracts"] = ",".join(criteria.contract_types)
        params.update(criteria.extra or {})
        return f"{self._base_url}{SEARCH_PATH}?{urlencode(params)}"

    def _read_page(self, url: str) -> Iterable[Dict[str, Any]]:
        """Rend les offres brutes d'une page ; une page illisible n'est pas fatale."""
        try:
            payload = extract_nuxt_payload(self._fetch_page(url))
        except DevalueError as exc:
            logger.warning("free-work: payload illisible sur %s (%s)", url, exc)
            return []
        except Exception as exc:  # transport : réseau, timeout, statut HTTP
            logger.warning("free-work: page inaccessible %s (%s)", url, exc)
            return []

        jobs = _locate_jobs(payload)
        if not jobs:
            logger.info("free-work: aucune offre trouvée sur %s", url)
        return jobs

    # -- Traduction vers le modèle pivot ---------------------------------

    def _to_offer(self, job: Dict[str, Any]) -> Optional[RawOffer]:
        """Traduit une offre Free-Work ; rend None si elle est inexploitable."""
        if not isinstance(job, dict):
            return None
        external_id, slug, title = job.get("id"), job.get("slug"), job.get("title")
        if not external_id or not slug or not title:
            return None

        low, high = parse_daily_rate(job.get("dailySalary"))
        company = job.get("company") or {}
        location = job.get("location") or {}

        try:
            return RawOffer(
                source=self.key,
                external_id=external_id,
                url=self._offer_url(job, slug),
                title=title,
                company=company.get("name") if isinstance(company, dict) else None,
                description=strip_html(job.get("description")),
                published_at=_parse_date(job.get("publishedAt")),
                daily_rate_min=low,
                daily_rate_max=high,
                duration_months=_duration_in_months(
                    job.get("durationValue"), job.get("durationPeriod")
                ),
                remote_mode=job.get("remoteMode"),
                location=location.get("label") if isinstance(location, dict) else None,
                contract_type=job.get("jobPostingType"),
                skills=_skill_names(job.get("skills")),
                raw=job,
            )
        except Exception as exc:  # schéma inattendu : on saute cette offre
            logger.warning("free-work: offre %s ignorée (%s)", external_id, exc)
            return None

    def _offer_url(self, job: Dict[str, Any], slug: str) -> str:
        """
        Construit le lien vers l'annonce.

        Free-Work range ses offres sous `/fr/tech-it/job-mission/<métier>/<slug>`.
        Le chemin de recherche `/jobs/<slug>` existe mais renvoie une 301 vers la
        liste : un lien construit ainsi ramène le visiteur à l'accueil.

        On prend donc en priorité le champ `to` que le site fournit lui-même, et
        on ne reconstruit qu'à défaut, à partir du métier rattaché à l'offre.
        """
        for card_key in ("cardProps", "cardAltProps"):
            card = job.get(card_key)
            if isinstance(card, dict):
                path = card.get("to")
                if isinstance(path, str) and path.startswith("/"):
                    return f"{self._base_url}{path}"

        job_family = job.get("job")
        if isinstance(job_family, dict) and job_family.get("slug"):
            return f"{self._base_url}{OFFER_PATH}/{job_family['slug']}/{slug}"

        # Dernier recours : la recherche filtrée sur le titre, qui reste utile
        # même si elle n'ouvre pas directement l'annonce.
        return f"{self._base_url}{SEARCH_PATH}?query={quote_plus(slug.replace('-', ' '))}"

    @staticmethod
    def _below_rate_floor(offer: RawOffer, floor: Optional[int]) -> bool:
        """Une offre sans TJM publié est conservée : l'absence n'est pas un refus."""
        if floor is None:
            return False
        midpoint = offer.rate_midpoint
        return midpoint is not None and midpoint < floor


# -- Utilitaires de parsing ---------------------------------------------


def _locate_jobs(payload: Any) -> List[Dict[str, Any]]:
    """
    Retrouve la liste d'offres dans le payload décodé.

    La clé exacte encode l'URL de recherche (`jobs-search-/fr/tech-it/jobs?...`),
    donc elle change à chaque requête : on cherche par *forme* plutôt que par nom.
    """
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            jobs = node.get("jobs")
            if isinstance(jobs, list) and "totalItems" in node:
                return [job for job in jobs if isinstance(job, dict)]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return []


def _parse_date(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _duration_in_months(value: Any, period: Any) -> Optional[int]:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    factor = _DURATION_IN_MONTHS.get(period or "month")
    return round(value * factor) if factor else None


def _skill_names(skills: Any) -> List[str]:
    if not isinstance(skills, list):
        return []
    return [
        skill["name"].strip()
        for skill in skills
        if isinstance(skill, dict) and isinstance(skill.get("name"), str) and skill["name"].strip()
    ]
