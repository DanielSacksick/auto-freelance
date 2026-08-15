"""
base.py — Contrat commun aux sources d'opportunités freelance.

Une source sait faire une seule chose : rendre une liste de `RawOffer` pour des
critères donnés. Elle ne persiste rien, ne score rien, ne rédige rien.

Le transport HTTP est **injecté** (`fetcher`) plutôt qu'importé : les tests
tournent hors-ligne, et une source peut être servie par requests, par un cache
ou par un navigateur piloté sans que son code de parsing change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from job_scanner.models import RawOffer

# Un fetcher prend une URL et rend le corps de la réponse en texte.
Fetcher = Callable[[str], str]


@dataclass(frozen=True)
class SearchCriteria:
    """Critères de recherche, exprimés indépendamment de toute plateforme."""

    query: str = "intelligence artificielle"
    max_pages: int = 1
    min_daily_rate: Optional[int] = None
    contract_types: tuple = ("contractor",)
    extra: dict = field(default_factory=dict)


class OfferSource(ABC):
    """Interface que toute plateforme doit implémenter."""

    #: Identifiant court et stable, stocké en base dans la colonne `source`.
    key: str = ""

    @abstractmethod
    def fetch(self, criteria: SearchCriteria) -> List[RawOffer]:
        """Rend les offres correspondant aux critères. Ne lève pas sur 0 résultat."""
        raise NotImplementedError
