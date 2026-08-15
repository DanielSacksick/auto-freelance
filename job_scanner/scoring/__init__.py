"""
scoring — Note d'adéquation d'une mission au profil du candidat.

Point d'entrée réel : `OfferScorer(profile).score(offer)`, où `profile` vient
de `config.AppConfig.candidate_profile(source)`. `score.py` (racine du
dépôt) orchestre l'appel sur un lot d'offres et applique le seuil
(`config.min_score_for(source)`).
"""

from __future__ import annotations

from job_scanner.scoring.profile import CandidateProfile, DEFAULT_PROFILE
from job_scanner.scoring.scorer import OfferScorer, ScoreResult

__all__ = ["OfferScorer", "ScoreResult", "CandidateProfile", "DEFAULT_PROFILE"]
