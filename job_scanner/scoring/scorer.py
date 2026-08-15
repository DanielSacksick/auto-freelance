"""
scorer.py — Note d'adéquation d'une mission au profil du candidat.

Le score est **explicable par construction** : chaque point attribué ou retiré
est consigné dans `reasons`, stocké en base (`fit_reasons`). L'utilisateur
doit pouvoir lire pourquoi une mission est remontée en tête, et corriger le
profil plutôt que de subir un score opaque.

Barème sur 100 :
    compétences cœur   45   ce que la mission demande vraiment
    similarité         25   proximité sémantique avec le profil
    TJM                15   par rapport au taux visé
    conditions         15   télétravail, durée
    pénalités         -40   stack hors périmètre

La similarité sémantique est **facultative** : si l'API d'embedding est
indisponible, son poids est redistribué et le scoring continue de tourner.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from job_scanner.models import RawOffer
from job_scanner.scoring.profile import CandidateProfile, DEFAULT_PROFILE

logger = logging.getLogger(__name__)

Embedder = Callable[[str], Optional[Sequence[float]]]

WEIGHT_SKILLS = 45.0
WEIGHT_SEMANTIC = 25.0
WEIGHT_RATE = 15.0
WEIGHT_CONDITIONS = 15.0
MAX_PENALTY = 40.0

# Nombre de compétences cœur au-delà duquel on considère la couverture totale.
_SKILL_SATURATION = 4.0


@dataclass(frozen=True)
class ScoreResult:
    """Note d'une mission, avec sa justification."""

    score: float
    reasons: Dict[str, Any]


class OfferScorer:
    """Note une mission de 0 à 100 au regard d'un profil."""

    def __init__(
        self,
        profile: CandidateProfile = DEFAULT_PROFILE,
        embedder: Optional[Embedder] = None,
    ):
        self._profile = profile
        self._embedder = embedder
        self._reference_vector: Optional[Sequence[float]] = None
        self._reference_failed = False

    def score(self, offer: RawOffer) -> ScoreResult:
        haystack = _normalize(_offer_text(offer))

        matched, skill_points = self._score_skills(haystack)
        penalties, penalty_points = self._score_penalties(haystack)
        rate_points, rate_label = self._score_rate(offer)
        condition_points, conditions = self._score_conditions(offer)
        similarity = self._semantic_similarity(offer)

        earned = skill_points + rate_points + condition_points
        available = WEIGHT_SKILLS + WEIGHT_RATE + WEIGHT_CONDITIONS

        if similarity is None:
            # Poids sémantique redistribué : le score reste comparable d'une offre à l'autre.
            total = earned / available * 100.0
        else:
            total = earned + similarity * WEIGHT_SEMANTIC
            total = total / (available + WEIGHT_SEMANTIC) * 100.0

        final = max(0.0, min(100.0, total - penalty_points))

        return ScoreResult(
            score=round(final, 2),
            reasons={
                "matched_skills": matched,
                "penalties": penalties,
                "rate": rate_label,
                "conditions": conditions,
                "semantic": round(similarity, 3) if similarity is not None else None,
            },
        )

    # -- Composantes du barème ------------------------------------------

    def _score_skills(self, haystack: str) -> tuple:
        """Somme les poids des compétences cœur détectées, avec saturation."""
        matched: List[str] = []
        weight_sum = 0.0

        for term, weight in self._profile.core_skills.items():
            if _contains_term(haystack, term):
                matched.append(term)
                weight_sum += weight

        # Saturation : au-delà de quelques compétences fortes, le gain s'aplatit.
        ratio = 1.0 - math.exp(-weight_sum / _SKILL_SATURATION)
        return _dedupe_terms(matched), ratio * WEIGHT_SKILLS

    def _score_penalties(self, haystack: str) -> tuple:
        found: List[str] = []
        weight_sum = 0.0

        for term, weight in self._profile.exclusions.items():
            if _contains_term(haystack, term):
                found.append(f"hors périmètre : {term}")
                weight_sum += weight

        ratio = 1.0 - math.exp(-weight_sum / 2.0)
        return found, ratio * MAX_PENALTY

    def _score_rate(self, offer: RawOffer) -> tuple:
        """
        Note le taux journalier au regard du taux visé.

        Règle absolue : **aucune conversion de devise**. Un montant libellé
        autrement que le profil n'est pas comparable, et le convertir avec un
        taux de change implicite ferait mentir le score. On rend alors la même
        note neutre qu'un budget non communiqué, en disant pourquoi.
        """
        midpoint = offer.rate_midpoint
        if midpoint is None:
            # Non communiqué : ni bonus ni malus, on accorde la moyenne.
            return WEIGHT_RATE * 0.6, "non communiqué"

        profile = self._profile
        currency = (offer.currency or profile.currency).upper()
        if currency != profile.currency.upper():
            return (
                WEIGHT_RATE * 0.6,
                f"{midpoint} {currency}/j — devise non comparable au profil "
                f"({profile.currency}), non converti",
            )

        if midpoint >= profile.target_rate:
            return WEIGHT_RATE, f"{midpoint} {currency}/j — au niveau visé"
        if midpoint <= profile.floor_rate:
            return 0.0, f"{midpoint} {currency}/j — sous le plancher"

        span = profile.target_rate - profile.floor_rate
        ratio = (midpoint - profile.floor_rate) / span
        return WEIGHT_RATE * ratio, f"{midpoint} {currency}/j"

    def _score_conditions(self, offer: RawOffer) -> tuple:
        points = 0.0
        notes: List[str] = []
        half = WEIGHT_CONDITIONS / 2

        if offer.remote_mode in self._profile.preferred_remote:
            points += half
            notes.append(f"télétravail {offer.remote_mode}")
        elif offer.remote_mode:
            notes.append(f"télétravail {offer.remote_mode}")
        else:
            points += half * 0.5  # inconnu : neutre

        duration = offer.duration_months
        if duration is None:
            points += half * 0.5
        elif duration <= self._profile.max_duration_months:
            points += half
            notes.append(f"{duration} mois")
        else:
            notes.append(f"{duration} mois — régie longue")

        return points, notes

    def _semantic_similarity(self, offer: RawOffer) -> Optional[float]:
        """Cosinus entre la mission et le profil, ou None si indisponible."""
        if self._embedder is None or self._reference_failed:
            return None

        reference = self._reference()
        if reference is None:
            return None

        try:
            vector = self._embedder(_offer_text(offer))
        except Exception as exc:
            logger.warning("scoring: embedding de l'offre impossible (%s)", exc)
            return None

        if not vector:
            return None
        return _cosine(reference, vector)

    def _reference(self) -> Optional[Sequence[float]]:
        """Vectorise le profil une seule fois par scorer."""
        if self._reference_vector is not None:
            return self._reference_vector
        try:
            self._reference_vector = self._embedder(self._profile.reference_text)
        except Exception as exc:
            logger.warning("scoring: embedding du profil impossible (%s)", exc)
            self._reference_failed = True
            return None

        if not self._reference_vector:
            self._reference_failed = True
            return None
        return self._reference_vector


# -- Utilitaires ---------------------------------------------------------


def _offer_text(offer: RawOffer) -> str:
    parts = [offer.title, " ".join(offer.skills or []), offer.description or ""]
    return " ".join(part for part in parts if part)


def _normalize(text: str) -> str:
    """Minuscules sans accents, pour comparer « IA générative » et « ia generative »."""
    lowered = (text or "").lower()
    decomposed = unicodedata.normalize("NFD", lowered)
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def _contains_term(haystack: str, term: str) -> bool:
    """
    Cherche un terme sur des frontières de mots.

    Indispensable pour éviter que « java » se déclenche sur « javascript »
    ou que « soc » se déclenche sur « société ».
    """
    pattern = r"(?<![a-z0-9])" + re.escape(_normalize(term)) + r"(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def _dedupe_terms(terms: List[str]) -> List[str]:
    """Retire les termes redondants (« agent ia » couvert par « agents ia »)."""
    ordered = sorted(set(terms), key=len, reverse=True)
    kept: List[str] = []
    for term in ordered:
        if not any(term in longer and term != longer for longer in kept):
            kept.append(term)
    return sorted(kept)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosinus ramené dans [0, 1]."""
    size = min(len(a), len(b))
    if not size:
        return 0.0
    dot = sum(a[i] * b[i] for i in range(size))
    norm_a = math.sqrt(sum(a[i] * a[i] for i in range(size)))
    norm_b = math.sqrt(sum(b[i] * b[i] for i in range(size)))
    if not norm_a or not norm_b:
        return 0.0
    return max(0.0, min(1.0, (dot / (norm_a * norm_b) + 1) / 2))
