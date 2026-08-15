"""
profile.py — Profil de référence contre lequel les missions sont notées.

Le profil est une **donnée**, pas de la logique : il décrit ce que le
freelance sait faire, ce qu'il vise et ce qu'il ne veut pas. `CandidateProfile`
est le contrat que `scoring/scorer.py` consomme ; il ne connaît aucun profil
en particulier.

Le contenu réel (compétences, TJM, exclusions) vient de `config.yml`, chargé
par `config.py` (`AppConfig.candidate_profile()`). Ce module ne définit que la
forme des données, pas leur contenu — c'est ce qui permet de publier ce dépôt
sans y laisser le profil de qui que ce soit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class CandidateProfile:
    """Ce que le scoring compare à chaque mission."""

    core_skills: Dict[str, float] = field(default_factory=dict)
    exclusions: Dict[str, float] = field(default_factory=dict)
    reference_text: str = ""

    # Conditions visées.
    target_rate: int = 600
    floor_rate: int = 400
    #: Devise dans laquelle `target_rate` et `floor_rate` sont exprimés. Une
    #: offre libellée autrement n'est **pas** convertie : elle est notée neutre
    #: (voir `scoring/scorer.py::_score_rate`).
    currency: str = "EUR"
    preferred_remote: Tuple[str, ...] = ("full", "partial")
    max_duration_months: int = 18

    @property
    def skill_terms(self) -> List[str]:
        return list(self.core_skills)


#: Profil vide, utilisé comme valeur par défaut par `OfferScorer` — jamais
#: destiné à noter une vraie mission (aucune compétence à matcher). Le
#: pipeline appelle toujours `AppConfig.candidate_profile()` en pratique.
DEFAULT_PROFILE = CandidateProfile()
