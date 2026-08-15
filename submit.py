#!/usr/bin/env python3
"""
submit.py — Soumission automatique des candidatures rédigées, sur les
plateformes qui le permettent (`config.AUTO_SUBMIT_PLATFORMS`).

Upwork et LinkedIn ne sont jamais soumis automatiquement (Connects payants /
pas de mapping de formulaire) : ils ressortent avec `submission=None`, à
candidater manuellement. Ne marque jamais `applied` lui-même — c'est
`pipeline.py` qui décide, sur la foi de `SubmissionResult.submitted`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from config import AUTO_SUBMIT_PLATFORMS
from job_scanner.submission.playwright import submit as playwright_submit

logger = logging.getLogger(__name__)


def submit_offers(
    drafted: List[Dict[str, Any]], config: Any, dry_run: bool = False
) -> List[Dict[str, Any]]:
    """
    Soumet chaque offre rédigée là où c'est automatisable.

    Args:
        drafted: sortie de `draft.py::draft_offers()` — chaque élément porte
            au moins `offer` (RawOffer) et `draft_body` (str).
        config: `AppConfig` chargée (headless, session_dir, quotas).
        dry_run: remplit le formulaire mais ne clique pas sur Envoyer.

    Returns:
        La même liste, chaque élément enrichi d'une clé `submission` :
        `None` si la plateforme n'est pas auto-submit-eligible ou si le quota
        journalier est atteint, sinon un `SubmissionResult`.
    """
    attempts_by_source: Dict[str, int] = {}
    results: List[Dict[str, Any]] = []

    for item in drafted:
        offer = item["offer"]
        source = getattr(offer, "source", None)
        entry = dict(item)

        if source not in AUTO_SUBMIT_PLATFORMS:
            entry["submission"] = None
            results.append(entry)
            continue

        quota = config.daily_quota_for(source)
        attempts = attempts_by_source.get(source, 0)
        if quota is not None and attempts >= quota:
            logger.info(
                "🚦 %s — quota journalier atteint (%d), soumission différée", source, quota
            )
            entry["submission"] = None
            results.append(entry)
            continue

        attempts_by_source[source] = attempts + 1
        result = playwright_submit(
            source=source,
            url=offer.url,
            body=item.get("draft_body", ""),
            headless=config.submission.headless,
            session_dir=str(config.session_dir()),
            dry_run=dry_run,
        )
        entry["submission"] = result
        results.append(entry)
        _log_outcome(offer, result)

    return results


def _log_outcome(offer: Any, result: Any) -> None:
    title = (getattr(offer, "title", "") or "?")[:60]
    logger.info("%s — %s", title, result.text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(
        "submit.py expose submit_offers(drafted, config, dry_run=False) ; il "
        "attend une liste d'offres déjà rédigées (voir draft.py / "
        "repo.offers_ready_to_submit()) et se lance via pipeline.py, pas "
        "directement."
    )
