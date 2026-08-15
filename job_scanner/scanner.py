"""
scanner.py — Orchestration du scan quotidien.

Le scanner interroge chaque source, agrège les offres et les confie au
repository. Il ne connaît ni HTTP ni SQL : sources et repository sont injectés.

Principe de robustesse : une plateforme en panne (503, refonte de page, réseau)
ne doit jamais faire perdre le scan des autres. L'échec est consigné dans le
rapport, pas propagé.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from job_scanner.models import RawOffer
from job_scanner.sources.base import OfferSource, SearchCriteria

logger = logging.getLogger(__name__)

_EMPTY_RESULT = {"seen": 0, "new": 0, "updated": 0}


class JobScanner:
    """Interroge les plateformes et persiste les missions trouvées."""

    def __init__(self, sources: Sequence[OfferSource], repo: Any):
        self._sources = list(sources)
        self._repo = repo

    def scan(self, criteria: Optional[SearchCriteria] = None) -> Dict[str, Any]:
        """
        Lance un scan complet.

        Returns:
            Un rapport : nombre d'offres par source (ou l'erreur rencontrée),
            total, et le résultat de la persistance.
        """
        criteria = criteria or SearchCriteria()
        offers: List[RawOffer] = []
        per_source: Dict[str, Dict[str, Any]] = {}

        for source in self._sources:
            try:
                found = source.fetch(criteria)
            except Exception as exc:
                logger.warning("scan: source %s en échec (%s)", source.key, exc)
                per_source[source.key] = {"offers": 0, "error": str(exc)}
                continue

            offers.extend(found)
            per_source[source.key] = {"offers": len(found)}

        persisted = self._repo.record_scan(offers) if offers else dict(_EMPTY_RESULT)

        return {
            "sources": per_source,
            "total_offers": len(offers),
            "persisted": persisted,
        }
