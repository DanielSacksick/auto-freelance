#!/usr/bin/env python3
"""
pipeline.py — Orchestrateur du pipeline auto-freelance.

    python3 pipeline.py                # scan → score → persist → draft → submit → notify
    python3 pipeline.py --dry-run      # scan + score seulement, AUCUN effet de bord
    python3 pipeline.py --scan-only    # scan + score + persist, pas de rédaction/soumission
    python3 pipeline.py --submit-only  # soumet les offres déjà rédigées en attente
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

from config import AppConfig, load_config
from draft import draft_offers
from notify import notify
from repo import SqliteRepo, offer_to_dict
from scan import run_scan
from score import score_offers
from submit import submit_offers

logger = logging.getLogger(__name__)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pipeline auto-freelance")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="scan + score seulement, aucun effet de bord (pas de persist, draft ou submit)",
    )
    parser.add_argument(
        "--scan-only", action="store_true",
        help="scan + score + persist, pas de rédaction ni de soumission",
    )
    parser.add_argument(
        "--submit-only", action="store_true",
        help="soumet uniquement les offres déjà rédigées en attente (pas de scan/score/draft)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    start = datetime.now()
    logger.info("=" * 40)
    logger.info("🤖 Auto-Freelance — %s", start.strftime("%d/%m/%Y %H:%M"))
    if args.dry_run:
        logger.info("🏁 Mode DRY RUN — aucun effet de bord")
    logger.info("=" * 40)

    config = load_config()

    if args.submit_only:
        return _run_submit_only(config)

    # 1. Scan --------------------------------------------------------------
    logger.info("\n📡 Phase 1 — Scan des plateformes")
    offers = run_scan(config)
    logger.info("  📦 %d offre(s) trouvée(s)", len(offers))

    # 2. Score ---------------------------------------------------------------
    logger.info("\n📊 Phase 2 — Scoring")
    scored = score_offers(offers, config, dry_run=args.dry_run)
    logger.info("  🏆 %d offre(s) au-dessus du seuil", len(scored))

    if args.dry_run:
        _print_dry_run_summary(offers, scored)
        return 0

    repo = SqliteRepo()
    try:
        # 3. Persist -----------------------------------------------------
        logger.info("\n💾 Phase 3 — Persistance")
        persisted = repo.record_scan(offers)
        logger.info(
            "  %d nouvelle(s), %d mise(s) à jour, %d déjà vue(s)",
            persisted["new"], persisted["updated"], persisted["seen"],
        )
        if scored:
            score_dicts = [
                offer_to_dict(item["offer"], fit_score=item["score"], fit_reasons=item["reasons"])
                for item in scored
            ]
            repo.record_scan(score_dicts)

        if args.scan_only:
            logger.info("\n🏁 Mode scan-only : pas de rédaction, pas de soumission")
            _final_report(offers, scored, drafted=[], submitted=[], notify_report=None)
            return 0

        # 4. Draft ---------------------------------------------------------
        logger.info("\n✍️ Phase 4 — Rédaction des candidatures")
        drafted = draft_offers(scored, config)
        logger.info("  %d candidature(s) rédigée(s)", len(drafted))
        if drafted:
            draft_dicts = [
                offer_to_dict(
                    item["offer"],
                    fit_score=item["score"],
                    fit_reasons=item["reasons"],
                    draft_body=item["draft_body"],
                    draft_model=item.get("draft_model", "unknown"),
                )
                for item in drafted
            ]
            repo.record_scan(draft_dicts)

        # 5. Submit (plateformes auto-submit-eligible uniquement) --------
        logger.info("\n🚀 Phase 5 — Soumission automatique")
        submitted = submit_offers(drafted, config, dry_run=False)
        applied_count = _mark_applied(repo, submitted)

        # 6. Notify ----------------------------------------------------------
        logger.info("\n📨 Phase 6 — Notification")
        notify_report = notify(repo, config, min_score=config.scoring.min_score_for_draft)

        _final_report(offers, scored, drafted, submitted, notify_report, applied_count)
    finally:
        repo.close()

    return 0


def _run_submit_only(config: AppConfig) -> int:
    """`--submit-only` : reprend les offres déjà rédigées, sans repasser par scan/score/draft."""
    repo = SqliteRepo()
    try:
        logger.info("\n🚀 --submit-only : soumission des offres déjà rédigées")
        ready = repo.offers_ready_to_submit()
        logger.info("  %d offre(s) prête(s) à soumettre", len(ready))

        submitted = submit_offers(ready, config, dry_run=False)
        applied_count = _mark_applied(repo, submitted)

        logger.info("\n" + "=" * 40)
        logger.info("✅ --submit-only terminé")
        logger.info("  🚀  Tentatives de soumission : %d", sum(1 for i in submitted if i.get("submission")))
        logger.info("  ✅  Confirmées : %d", applied_count)
        logger.info("=" * 40)
        return 0
    finally:
        repo.close()


def _mark_applied(repo: SqliteRepo, submitted: List[Dict[str, Any]]) -> int:
    """Marque `applied` en base pour chaque soumission confirmée. Rend le nombre marqué."""
    applied_count = 0
    for item in submitted:
        result = item.get("submission")
        if result is not None and getattr(result, "submitted", False):
            offer = item["offer"]
            repo.mark_applied(offer.source, offer.external_id, result.to_dict())
            applied_count += 1
    return applied_count


def _print_dry_run_summary(offers: List[Any], scored: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 40)
    print("🏁 DRY RUN — résumé (aucun effet de bord)")
    print("=" * 40)
    print(f"  📡  Offres scannées : {len(offers)}")
    print(f"  📊  Au-dessus du seuil : {len(scored)}")
    for item in scored[:10]:
        offer = item["offer"]
        print(f"    · [{item['score']:.0f}/100] {offer.title[:60]} ({offer.source})")
    print("=" * 40)


def _final_report(
    offers: List[Any],
    scored: List[Dict[str, Any]],
    drafted: List[Dict[str, Any]],
    submitted: List[Dict[str, Any]],
    notify_report: Optional[Dict[str, Any]],
    applied_count: int = 0,
) -> None:
    attempted = sum(1 for item in submitted if item.get("submission") is not None)
    logger.info("\n" + "=" * 40)
    logger.info("✅ Pipeline terminé")
    logger.info("  📡  Offres scannées : %d", len(offers))
    logger.info("  📊  Au-dessus du seuil : %d", len(scored))
    logger.info("  ✍️   Candidatures rédigées : %d", len(drafted))
    logger.info("  🚀  Tentatives de soumission : %d (dont %d confirmée(s))", attempted, applied_count)
    if notify_report is not None:
        logger.info("  📨  Notifications envoyées : %d", notify_report.get("sent", 0))
    logger.info("=" * 40)


if __name__ == "__main__":
    raise SystemExit(main())
