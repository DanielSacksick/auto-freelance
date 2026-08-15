"""
draft.py — Drafts cover letters for scored offers.

Thin orchestration over `job_scanner/drafting/`: for each scored offer, in
score order, write a cover letter in the platform's language
(`config.language_for(source)`), using the candidate's own identity and
background (`config.profile`, `config.drafting`) — never anything hardcoded
here. Two config-driven guardrails are enforced defensively, on top of
whatever `score.py` already filtered:

- `config.min_score_for(source)`: offers under the platform's threshold are
  skipped (re-checked here in case `scored` wasn't pre-filtered).
- `config.daily_quota_for(source)`: once a platform's quota of drafts is hit
  for this run, further offers for that platform are skipped rather than
  drafted for nothing — some platforms (e.g. Upwork) charge per application,
  so drafting what won't be sent wastes tokens for no reason.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from job_scanner.drafting.writer import ApplicationWriter, default_llm_caller

logger = logging.getLogger(__name__)


def draft_offers(scored: List[Dict[str, Any]], config: Any) -> List[Dict[str, Any]]:
    """
    Drafts cover letters for `scored` offers.

    Args:
        scored: `[{"offer": RawOffer, "score": float, "reasons": dict}, ...]`,
            the shape produced by `score.py::score_offers()`.
        config: an `AppConfig` (see `config.py::load_config()`).

    Returns:
        `[{"offer", "score", "reasons", "draft_body", "draft_model"}, ...]`
        for offers where drafting succeeded, in the same order as `scored`
        (already score-sorted by `score_offers`). Offers below threshold,
        over quota, or that fail to produce a valid draft are silently
        skipped (logged, not raised) — a partial batch beats a crashed run.
    """
    llm = default_llm_caller(model=config.drafting.model, temperature=config.drafting.temperature)
    writers: Dict[str, ApplicationWriter] = {}
    quota_used: Dict[str, int] = {}
    drafted: List[Dict[str, Any]] = []

    for item in scored:
        offer = item["offer"]
        source = getattr(offer, "source", "")
        title = getattr(offer, "title", "?")[:40]

        threshold = config.min_score_for(source)
        if item["score"] < threshold:
            logger.debug("draft: %s — %.1f/100 sous le seuil %.1f, ignorée", title, item["score"], threshold)
            continue

        quota = config.daily_quota_for(source)
        if quota is not None and quota_used.get(source, 0) >= quota:
            logger.info("draft: quota atteint pour %s (%d/j), « %s » ignorée", source, quota, title)
            continue

        language = config.language_for(source)
        writer = writers.get(language)
        if writer is None:
            writer = ApplicationWriter(
                llm=llm,
                name=config.profile.name,
                company=config.profile.company,
                portfolio_url=config.profile.portfolio_url,
                bio=config.drafting.bio,
                past_projects=config.drafting.past_projects,
                model_label=config.drafting.model,
                language=language,
            )
            writers[language] = writer

        matched_skills = item.get("reasons", {}).get("matched_skills", [])
        try:
            draft = writer.write(offer, matched_skills=matched_skills)
        except Exception as exc:
            logger.warning("draft: rédaction impossible pour « %s » (%s)", title, exc)
            continue

        if draft is None:
            logger.info("draft: échec de rédaction pour « %s »", title)
            continue

        quota_used[source] = quota_used.get(source, 0) + 1
        drafted.append({
            "offer": offer,
            "score": item["score"],
            "reasons": item["reasons"],
            "draft_body": draft.body,
            "draft_model": draft.model,
        })
        logger.info("draft: « %s » — %.1f/100 — %d signes", title, item["score"], len(draft.body))

    logger.info("draft: %d candidature(s) rédigée(s) sur %d offre(s) scorée(s)", len(drafted), len(scored))
    return drafted


if __name__ == "__main__":
    # draft.py needs already-scored offers as input, which come from
    # scan.py + score.py. It is meant to be called as a library function
    # (`draft_offers`) from pipeline.py, which owns the full
    # scan -> score -> draft -> submit flow.
    print(
        "draft.py exposes draft_offers(scored, config) for pipeline.py to call.\n"
        "Run the full pipeline via: python3 pipeline.py"
    )
