"""
models.py — Modèle pivot d'une opportunité freelance.

`RawOffer` est le contrat commun à toutes les sources (Free-Work, Comet,
Collective, LinkedIn…). Chaque source traduit son format propre vers ce modèle ;
tout ce qui vit en aval (dédup, scoring, rédaction) n'en connaît pas d'autre.

C'est ce qui permet d'ajouter une plateforme sans toucher au reste du pipeline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class RawOffer(BaseModel):
    """Une mission freelance normalisée, telle que publiée par une plateforme."""

    # Identité — (source, external_id) est la clé de déduplication.
    source: str
    external_id: str
    url: str

    title: str
    company: Optional[str] = None
    description: Optional[str] = None
    published_at: Optional[datetime] = None

    # Rémunération, exprimée en TJM. Les deux bornes sont égales si taux unique.
    daily_rate_min: Optional[int] = None
    daily_rate_max: Optional[int] = None
    currency: str = "EUR"

    duration_months: Optional[int] = None
    remote_mode: Optional[str] = None       # full | partial | none
    location: Optional[str] = None
    contract_type: Optional[str] = None     # contractor, permanent…
    skills: List[str] = Field(default_factory=list)

    # Charge utile d'origine, conservée pour rejouer un parsing sans re-scanner.
    raw: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", "company", "location", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("external_id", mode="before")
    @classmethod
    def _coerce_id(cls, value: Any) -> Any:
        return str(value) if value is not None else value

    @property
    def rate_midpoint(self) -> Optional[int]:
        """TJM représentatif, utilisé pour filtrer sur un plancher."""
        bounds = [b for b in (self.daily_rate_min, self.daily_rate_max) if b is not None]
        return sum(bounds) // len(bounds) if bounds else None
