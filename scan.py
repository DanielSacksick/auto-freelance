#!/usr/bin/env python3
"""
scan.py — Lance un scan des plateformes activées et rend les `RawOffer` trouvées.

Ne persiste rien : c'est le rôle de `repo.py`, appelé en aval par
`pipeline.py`. Ce module se contente d'assembler les sources activées dans
`config.yml`, de lancer `JobScanner` et de rendre la liste brute des offres.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from config import AppConfig, load_config
from job_scanner.fetcher import HttpFetcher
from job_scanner.models import RawOffer
from job_scanner.scanner import JobScanner
from job_scanner.sources.base import Fetcher, OfferSource, SearchCriteria
from job_scanner.sources.freework import FreeWorkSource
from job_scanner.sources.freelance_informatique import (
    FreelanceInfoConfig,
    FreelanceInformatiqueSource,
)
from job_scanner.sources.freelancermap import (
    FreelancermapConfig,
    FreelancermapSource,
)
from job_scanner.sources.linkedin import LinkedInJobsSource

logger = logging.getLogger(__name__)


class _CollectorRepo:
    """Repo minimal en mémoire, juste assez pour satisfaire `JobScanner`.

    `JobScanner` attend un objet `.record_scan(offers)` : celui-ci se contente
    de collecter les offres trouvées, sans aucune persistance. La persistance
    réelle (dédup, statuts) vit dans `repo.py`, appelée par `pipeline.py`.
    """

    def __init__(self) -> None:
        self.offers: List[RawOffer] = []

    def record_scan(self, offers: List[RawOffer]) -> Dict[str, int]:
        self.offers.extend(offers)
        return {"seen": len(offers), "new": len(offers), "updated": 0}


def _build_sources(config: AppConfig, fetcher: Fetcher) -> List[OfferSource]:
    """Instancie une source par plateforme activée (`config.enabled_platforms()`).

    Chaque source a sa propre signature de constructeur (fetcher seul,
    fetcher + config dédiée, ou aucun argument pour LinkedIn) : on la respecte
    plateforme par plateforme plutôt que de forcer une fabrique générique.
    """
    sources: List[OfferSource] = []
    enabled = set(config.enabled_platforms())

    if "freework" in enabled:
        sources.append(FreeWorkSource(fetcher=fetcher))

    if "freelance-informatique" in enabled:
        sources.append(
            FreelanceInformatiqueSource(fetcher=fetcher, config=FreelanceInfoConfig.from_env())
        )

    if "freelancermap" in enabled:
        sources.append(
            FreelancermapSource(fetcher=fetcher, config=FreelancermapConfig.from_env())
        )

    if "linkedin" in enabled:
        # LinkedInJobsSource ne consomme pas le fetcher commun : elle pilote son
        # propre navigateur Playwright avec une session authentifiée (voir
        # job_scanner/sources/linkedin.py). Sans session exportée pour
        # "linkedin", elle se journalise et rend une liste vide.
        sources.append(LinkedInJobsSource())

    if "upwork" in enabled:
        if os.environ.get("UPWORK_CLIENT_ID"):
            try:
                from job_scanner.sources.upwork import UpworkSearchConfig, UpworkSource

                sources.append(UpworkSource(config=UpworkSearchConfig.from_env()))
                logger.info("scan: upwork activé")
            except Exception as exc:  # identifiants invalides, dépendance manquante…
                logger.warning("scan: upwork indisponible (%s)", exc)
        else:
            logger.warning(
                "scan: upwork activé dans config.yml mais UPWORK_CLIENT_ID absent "
                "de l'environnement — plateforme ignorée pour ce scan"
            )

    return sources


def run_scan(config: AppConfig) -> List[RawOffer]:
    """Scanne toutes les plateformes activées, rend la liste des offres brutes."""
    fetcher = HttpFetcher()
    sources = _build_sources(config, fetcher)
    if not sources:
        logger.warning("scan: aucune plateforme activée dans config.yml")
        return []

    collector = _CollectorRepo()
    scanner = JobScanner(sources=sources, repo=collector)
    criteria = SearchCriteria(
        query=config.search.query,
        max_pages=config.search.max_pages,
        min_daily_rate=config.search.min_daily_rate,
    )

    report = scanner.scan(criteria)
    for key, data in report["sources"].items():
        if isinstance(data, dict) and data.get("error"):
            logger.warning("scan: %s en échec (%s)", key, data["error"])
        else:
            logger.info("scan: %s — %d offre(s)", key, (data or {}).get("offers", 0))

    return collector.offers


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = load_config()
    found = run_scan(cfg)

    per_platform: Dict[str, int] = {}
    for offer in found:
        per_platform[offer.source] = per_platform.get(offer.source, 0) + 1

    print("\n📡 Scan terminé")
    for source in sorted(per_platform):
        print(f"  · {source:<24} {per_platform[source]}")
    print(f"  Total : {len(found)} offre(s)")
