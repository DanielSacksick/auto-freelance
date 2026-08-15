"""
score.py — Scores scanned offers against the candidate's profile.

Thin orchestration over `job_scanner/scoring/`: for each offer, build the
right `CandidateProfile` for its platform (rates/currency can be overridden
per-platform, e.g. Upwork in USD — see `config.py`'s `AppConfig.
candidate_profile()`), score it with `OfferScorer`, and keep it if it clears
that platform's `min_score_for_draft` threshold.

This module owns no thresholds of its own: `config.yml` (via `AppConfig.
min_score_for(source)`) is the only source of truth for what counts as a good
enough match. That is what lets someone fork this repo and tune scoring
without touching this file.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from job_scanner.models import RawOffer
from job_scanner.scoring.scorer import OfferScorer

logger = logging.getLogger(__name__)


def score_offers(offers: List[RawOffer], config: Any, dry_run: bool = False) -> List[Dict[str, Any]]:
    """
    Scores `offers` against `config`'s candidate profile.

    Args:
        offers: raw offers, typically fresh from `scan.py`.
        config: an `AppConfig` (see `config.py::load_config()`).
        dry_run: when True, every offer is kept regardless of score, so a
            dry run stays informative even if nothing clears the bar. When
            False (the normal path), offers below `config.min_score_for
            (offer.source)` are dropped.

    Returns:
        `[{"offer": RawOffer, "score": float, "reasons": dict}, ...]`,
        sorted by score descending.
    """
    scored: List[Dict[str, Any]] = []

    for offer in offers:
        source = getattr(offer, "source", "")
        try:
            profile = config.candidate_profile(source)
            result = OfferScorer(profile=profile).score(offer)
        except Exception as exc:
            logger.warning("score: impossible de noter %r (%s)", getattr(offer, "title", "?"), exc)
            continue

        threshold = config.min_score_for(source)
        if result.score < threshold and not dry_run:
            logger.debug(
                "score: %s — %.1f/100 sous le seuil %.1f (%s)",
                getattr(offer, "title", "?")[:40], result.score, threshold, source,
            )
            continue

        scored.append({"offer": offer, "score": result.score, "reasons": result.reasons})

    scored.sort(key=lambda item: item["score"], reverse=True)
    logger.info("score: %d offre(s) retenue(s) sur %d", len(scored), len(offers))
    return scored


if __name__ == "__main__":
    # score.py needs scanned offers as input, which come from scan.py. It is
    # meant to be called as a library function (`score_offers`) from
    # pipeline.py, which owns the full scan -> score -> draft -> submit flow.
    # Running this file directly has nothing to score on its own.
    print(
        "score.py exposes score_offers(offers, config, dry_run=False) for pipeline.py to call.\n"
        "Run the full pipeline via: python3 pipeline.py"
    )
