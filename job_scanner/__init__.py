"""
job_scanner — Détection quotidienne des missions freelance.

Pipeline : sources → modèle pivot `RawOffer` → dédup et persistance.
Le scoring et la rédaction des candidatures viennent en aval (phase suivante).

Ajouter une plateforme = un fichier dans `sources/` + une entrée dans
`SOURCE_REGISTRY`. Rien d'autre à modifier (principe ouvert/fermé).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

from job_scanner.fetcher import HttpFetcher
from job_scanner.models import RawOffer
from job_scanner.scanner import JobScanner
from job_scanner.sources.base import Fetcher, OfferSource, SearchCriteria
from job_scanner.sources.freelance_informatique import (
    FreelanceInfoConfig,
    FreelanceInformatiqueSource,
)
from job_scanner.sources.freelancermap import (
    FreelancermapConfig,
    FreelancermapSource,
)
from job_scanner.sources.freework import FreeWorkSource
from job_scanner.sources.upwork import UpworkSearchConfig, UpworkSource

__all__ = [
    "RawOffer",
    "JobScanner",
    "SearchCriteria",
    "OfferSource",
    "SOURCE_REGISTRY",
    "build_scanner",
]

#: Plateformes disponibles, par clé. Une fabrique prend un fetcher et rend la source.
#: Upwork n'utilise pas ce fetcher (GET seul) : il parle GraphQL en POST
#: authentifié et résout son client au premier scan.
SOURCE_REGISTRY: Dict[str, Callable[[Fetcher], OfferSource]] = {
    "freework": lambda fetcher: FreeWorkSource(fetcher=fetcher),
    "freelance-informatique": lambda fetcher: FreelanceInformatiqueSource(
        fetcher=fetcher, config=FreelanceInfoConfig.from_env()
    ),
    "freelancermap": lambda fetcher: FreelancermapSource(
        fetcher=fetcher, config=FreelancermapConfig.from_env()
    ),
    "upwork": lambda fetcher: UpworkSource(config=UpworkSearchConfig.from_env()),
}


def build_scanner(
    source_keys: Optional[Sequence[str]] = None,
    repo: Any = None,
    fetcher: Optional[Fetcher] = None,
) -> JobScanner:
    """
    Assemble un scanner prêt à l'emploi.

    Args:
        source_keys: plateformes à interroger (toutes par défaut).
        repo: repository de persistance (OpportunitiesRepo par défaut, importé
              paresseusement pour que le module reste utilisable sans base).
        fetcher: transport HTTP, injectable pour les tests.

    Raises:
        ValueError: si une clé de source est inconnue.
    """
    keys = list(source_keys) if source_keys else list(SOURCE_REGISTRY)
    unknown = [key for key in keys if key not in SOURCE_REGISTRY]
    if unknown:
        raise ValueError(
            f"source(s) inconnue(s) : {', '.join(unknown)}. "
            f"Disponibles : {', '.join(sorted(SOURCE_REGISTRY))}"
        )

    shared_fetcher = fetcher or HttpFetcher()
    sources = [SOURCE_REGISTRY[key](shared_fetcher) for key in keys]

    if repo is None:
        from core.db.repositories.opportunities import OpportunitiesRepo

        repo = OpportunitiesRepo()

    return JobScanner(sources=sources, repo=repo)
