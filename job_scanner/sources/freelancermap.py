"""
freelancermap.py — Source freelancermap, le gisement de missions IT en Europe.

Plateforme allemande, mais qui publie des projets dans toute l'Europe. Elle
ouvre un second bassin géographique là où Free-Work et Freelance-Informatique
couvrent la France.

Son `robots.txt` n'interdit rien (`Disallow:` vide) et les pages de recherche
sont servies en clair, sans challenge anti-bot. Comme Free-Work, l'application
dépose ses données dans la page : un `<script class="js-react-on-rails-component">`
porte le composant `ProjectSearch`, dont `initialResults` contient les projets
**déjà structurés** — titre, description, société, ville, pays, durée, dates.

Conséquence : **une requête HTTP par page de résultats**, pas une par annonce,
et aucun parsing HTML fragile.

Deux limites, constatées sur des données réelles :

- `budget` est systématiquement nul sur la recherche. `daily_rate_*` reste donc
  vide plutôt qu'inventé, et le scoring traite l'absence comme neutre.
- `skills` est vide sur la recherche. Le scoring s'appuie sur le titre et la
  description, qui sont complets.

Le site mélange deux natures de contrat : `contracting` (mission freelance) et
`employee_leasing` (portage et intérim). Seul le premier est retenu.
"""

from __future__ import annotations

import json
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

BASE_URL = "https://www.freelancermap.com"
# `/projects` est la route du formulaire de recherche (`config.routes.formAction`).
# `/it-projects.html` existe et répond 200, mais **ignore silencieusement** le
# paramètre `query` : elle rend les derniers projets publiés, tous domaines
# confondus. S'y tromper remplit la base de missions sans rapport sans qu'aucune
# erreur ne le signale.
SEARCH_PATH = "/projects"
PROJECT_PATH = "/project"

# Le composant React qui porte la liste des projets.
SEARCH_COMPONENT = "ProjectSearch"

# Termes cherchés par défaut. Le moteur est plein texte : on reste sur le
# vocabulaire international, la plateforme publie en anglais et en allemand.
DEFAULT_TERMS: Tuple[str, ...] = (
    "AI agent",
    "LLM",
    "RAG",
    "generative AI",
    "MCP",
)

# `contracting` est la mission freelance. `employee_leasing` est du portage
# ou de l'intérim : même page, autre métier, hors périmètre freelance.
DEFAULT_CONTRACT_TYPES: Tuple[str, ...] = ("contracting",)

_DURATION_RE = re.compile(r"(\d+)\s*(month|monat|jahr|year|week|woche|tag|day)", re.I)
_MONTHS_PER_UNIT = {
    "month": 1, "monat": 1,
    "year": 12, "jahr": 12,
    "week": 0.25, "woche": 0.25,
    "day": 1 / 20, "tag": 1 / 20,
}


@dataclass(frozen=True)
class FreelancermapConfig:
    """Termes cherchés et natures de contrat retenues."""

    terms: Tuple[str, ...] = DEFAULT_TERMS
    contract_types: Tuple[str, ...] = DEFAULT_CONTRACT_TYPES
    page_size: int = 22

    @classmethod
    def from_env(cls) -> "FreelancermapConfig":
        """Lit `FREELANCERMAP_TERMS`, une liste séparée par des virgules."""
        defaults = cls()
        raw = os.environ.get("FREELANCERMAP_TERMS")
        if not raw:
            return defaults
        terms = tuple(term.strip() for term in raw.split(",") if term.strip())
        return replace(defaults, terms=terms or defaults.terms)


class FreelancermapSource(OfferSource):
    """Lit les projets freelance publiés sur freelancermap."""

    key = "freelancermap"

    def __init__(
        self,
        fetcher: Fetcher,
        config: Optional[FreelancermapConfig] = None,
        base_url: str = BASE_URL,
    ):
        self._fetch_page = fetcher
        self._config = config or FreelancermapConfig()
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
            projects = self._read_page(self._build_url(term, page), term)
            for project in projects:
                offer = self._to_offer(project)
                if offer is not None and self._is_wanted_contract(project):
                    found.append(offer)

            # Une page incomplète est la dernière : inutile d'en demander une autre.
            if len(projects) < self._config.page_size:
                break

        return found

    def _build_url(self, term: str, page: int) -> str:
        params: Dict[str, Any] = {"query": term, "sort": 1}
        if page > 1:
            params["pagenr"] = page
        return f"{self._base_url}{SEARCH_PATH}?{urlencode(params)}"

    def _read_page(self, url: str, term: str) -> List[Dict[str, Any]]:
        """Rend les projets bruts d'une page ; une page illisible n'est pas fatale."""
        try:
            html = self._fetch_page(url)
        except Exception as exc:  # réseau, timeout, statut HTTP
            logger.warning("freelancermap: page inaccessible %s (%s)", url, exc)
            return []

        payload = _extract_search_payload(html)
        if payload is None:
            logger.info("freelancermap: payload absent ou illisible sur %s", url)
            return []

        if not _query_was_applied(payload, term):
            logger.warning(
                "freelancermap: le terme « %s » n'a pas été appliqué par le site, "
                "page ignorée pour ne pas ramener de projets hors sujet",
                term,
            )
            return []

        results = payload.get("initialResults")
        if not isinstance(results, list):
            return []
        return [project for project in results if isinstance(project, dict)]

    # -- Traduction vers le modèle pivot ---------------------------------

    def _to_offer(self, project: Dict[str, Any]) -> Optional[RawOffer]:
        """Traduit un projet freelancermap ; rend None s'il est inexploitable."""
        external_id, title = project.get("id"), project.get("title")
        url = self._project_url(project)
        if not external_id or not title or not url:
            return None

        contract = project.get("projectContractType") or {}
        contract = contract if isinstance(contract, dict) else {}

        try:
            return RawOffer(
                source=self.key,
                external_id=external_id,
                url=url,
                title=title,
                company=project.get("company") or None,
                description=(project.get("description") or "").strip() or None,
                published_at=_parse_date(project.get("created")),
                # Le budget n'est pas publié sur la recherche : ne rien inventer.
                daily_rate_min=None,
                daily_rate_max=None,
                currency="EUR",
                duration_months=_duration_in_months(project),
                remote_mode=remote_mode_from_percent(contract.get("remoteInPercent")),
                location=_location(project),
                contract_type=contract.get("type") or None,
                skills=_skill_names(project.get("skills")),
                raw=project,
            )
        except Exception as exc:  # schéma inattendu : on saute ce projet
            logger.warning("freelancermap: projet %s ignoré (%s)", external_id, exc)
            return None

    def _project_url(self, project: Dict[str, Any]) -> Optional[str]:
        """
        Construit le lien vers le projet.

        Le champ `url` du payload est systématiquement nul ; c'est `links.project`
        que le site utilise lui-même. On ne reconstruit à partir du slug qu'à
        défaut — même leçon que sur Free-Work, où un chemin deviné renvoyait le
        visiteur vers la page d'accueil.
        """
        links = project.get("links")
        if isinstance(links, dict):
            path = links.get("project")
            if isinstance(path, str) and path.startswith("/"):
                return f"{self._base_url}{path}"

        slug = project.get("slug")
        if isinstance(slug, str) and slug.strip():
            return f"{self._base_url}{PROJECT_PATH}/{slug.strip()}"
        return None

    def _is_wanted_contract(self, project: Dict[str, Any]) -> bool:
        """
        Écarte le portage et l'intérim, qui partagent la page avec les missions.

        Un projet sans nature de contrat déclarée est conservé : l'absence
        d'information n'est pas un refus.
        """
        contract = project.get("projectContractType")
        if not isinstance(contract, dict):
            return True
        kind = contract.get("type")
        if not kind:
            return True
        return kind in self._config.contract_types


# -- Utilitaires de parsing ----------------------------------------------


def _extract_search_payload(html: str) -> Optional[Dict[str, Any]]:
    """Sort le JSON du composant `ProjectSearch` déposé dans la page."""
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return None

    for script in soup.select("script.js-react-on-rails-component"):
        if script.get("data-component-name") != SEARCH_COMPONENT:
            continue
        try:
            payload = json.loads(script.string or "", strict=False)
        except (ValueError, TypeError) as exc:
            logger.warning("freelancermap: payload %s illisible (%s)", SEARCH_COMPONENT, exc)
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _query_was_applied(payload: Dict[str, Any], term: str) -> bool:
    """
    Vérifie que le site a bien appliqué la recherche demandée.

    Le payload rappelle le filtre retenu dans `initialState.filter.query`. Sans
    ce contrôle, une route qui ignore le paramètre rend les derniers projets
    publiés — 22 missions SAP ou paie que rien ne distingue d'un vrai résultat,
    et qui finiraient en base comme si elles avaient matché.
    """
    state = payload.get("initialState")
    if not isinstance(state, dict):
        return False
    filters = state.get("filter")
    if not isinstance(filters, dict):
        return False

    applied = filters.get("query")
    if not isinstance(applied, str):
        return False
    return applied.strip().casefold() == (term or "").strip().casefold()


def remote_mode_from_percent(percent: Any) -> Optional[str]:
    """Traduit un pourcentage de télétravail dans le vocabulaire du modèle pivot."""
    if not isinstance(percent, (int, float)):
        return None
    if percent >= 100:
        return "full"
    if percent > 0:
        return "partial"
    return "none"


def _location(project: Dict[str, Any]) -> Optional[str]:
    city = project.get("city")
    country = project.get("country")
    country_name = country.get("name") if isinstance(country, dict) else None
    parts = [part.strip() for part in (city, country_name) if isinstance(part, str) and part.strip()]
    return ", ".join(parts) or None


def _duration_in_months(project: Dict[str, Any]) -> Optional[int]:
    """Préfère la durée numérique ; retombe sur le libellé quand elle manque."""
    duration = project.get("duration")
    if isinstance(duration, (int, float)) and duration > 0:
        return int(duration)

    label = project.get("durationText")
    if not isinstance(label, str):
        return None
    match = _DURATION_RE.search(label)
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2).lower()
    factor = _MONTHS_PER_UNIT.get(unit)
    if not factor or value <= 0:
        return None
    return max(1, round(value * factor))


def _skill_names(skills: Any) -> List[str]:
    """La recherche rend une liste vide ; la fiche projet, elle, est renseignée."""
    if not isinstance(skills, list):
        return []
    names: List[str] = []
    for skill in skills:
        if isinstance(skill, dict):
            label = skill.get("nameEn") or skill.get("name") or skill.get("nameDe")
        else:
            label = skill
        if isinstance(label, str) and label.strip():
            names.append(label.strip())
    return names


def _parse_date(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
