"""
config.py — Configuration utilisateur du pipeline auto-freelance.

Toute donnée propre à une personne (TJM, compétences, plateformes, ton de la
lettre de motivation) vit dans `config.yml`, jamais en dur dans le code. Ce
module ne fait que charger ce fichier et le traduire vers les objets attendus
par le reste du pipeline (`CandidateProfile` pour le scoring, dicts simples
pour la rédaction et la soumission).

`config.yml` est ignoré par git (voir `.gitignore`) : c'est une donnée
personnelle, pas du code. `config.example.yml` est le gabarit versionné ;
`onboard.py` le copie et le remplit à l'usage.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(os.environ.get("AUTOFREELANCE_CONFIG", "config.yml"))

# Plateformes de scan connues. `sources` (scan) et `submission` (candidature
# automatique) n'ont pas la même couverture : Upwork se scanne mais ne se
# soumet pas automatiquement (Connects payants), voir
# job_scanner/submission/forms/__init__.py.
KNOWN_PLATFORMS = (
    "freework",
    "freelance-informatique",
    "freelancermap",
    "upwork",
    "linkedin",
)

# Plateformes pour lesquelles un FormMapper Playwright existe : seules celles-ci
# peuvent être soumises automatiquement, quelle que soit la config. LinkedIn
# n'auto-soumet que l'Easy Apply (pas le bouton "Apply" externe) et abandonne
# proprement dès qu'une question de sélection ne peut pas être répondue sans
# inventer une réponse (voir job_scanner/submission/forms/linkedin.py).
AUTO_SUBMIT_PLATFORMS = ("freework", "freelance-informatique", "freelancermap", "linkedin")


class ConfigError(RuntimeError):
    """Configuration absente ou invalide — mieux vaut échouer tôt et clairement."""


@dataclass
class ProfileConfig:
    name: str = ""
    company: str = ""
    email: str = ""
    portfolio_url: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    cv_path: str = ""
    #: Langue par défaut de rédaction (fr | en). Une plateforme anglophone
    #: (Upwork) peut la redéfinir via `platforms.<key>.language`.
    language: str = "fr"


@dataclass
class RatesConfig:
    target_daily_rate: int = 600
    floor_daily_rate: int = 400
    currency: str = "EUR"


@dataclass
class SkillsConfig:
    #: {terme: poids 0-1}. Le poids traduit la force du signal, pas la maîtrise.
    core: Dict[str, float] = field(default_factory=dict)
    #: Signaux d'inadéquation — stack ou missions hors périmètre.
    exclusions: Dict[str, float] = field(default_factory=dict)
    #: Texte court utilisé pour la similarité sémantique optionnelle
    #: (`scoring/scorer.py`, embedder facultatif) et repris dans les prompts
    #: de rédaction comme résumé du profil.
    reference_text: str = ""


@dataclass
class ConditionsConfig:
    preferred_remote: List[str] = field(default_factory=lambda: ["full", "partial"])
    max_duration_months: int = 18


@dataclass
class PlatformConfig:
    enabled: bool = True
    #: Redéfinitions optionnelles pour cette plateforme (ex. Upwork en USD).
    target_daily_rate: Optional[int] = None
    floor_daily_rate: Optional[int] = None
    currency: Optional[str] = None
    language: Optional[str] = None
    min_score_for_draft: Optional[float] = None
    daily_quota: Optional[int] = None


@dataclass
class SearchConfig:
    query: str = "intelligence artificielle OR IA OR agent OR LLM"
    min_daily_rate: Optional[int] = None
    max_pages: int = 2


@dataclass
class ScoringConfig:
    min_score_for_draft: float = 60.0


@dataclass
class DraftingConfig:
    #: Court résumé (bio) injecté dans le prompt de rédaction.
    bio: str = ""
    #: Réalisations mobilisables : [{"domain": "...", "description": "..."}].
    #: Le rédacteur choisit celle la plus proche du domaine de l'annonce.
    past_projects: List[Dict[str, str]] = field(default_factory=list)
    #: Modèle OpenRouter utilisé pour la rédaction.
    model: str = "anthropic/claude-3.5-sonnet"
    temperature: float = 0.6


@dataclass
class SubmissionConfig:
    headless: bool = True
    session_dir: str = "~/.auto-freelance/sessions"


@dataclass
class NotificationsConfig:
    console: bool = True
    telegram_enabled: bool = False


@dataclass
class AppConfig:
    profile: ProfileConfig = field(default_factory=ProfileConfig)
    rates: RatesConfig = field(default_factory=RatesConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    conditions: ConditionsConfig = field(default_factory=ConditionsConfig)
    platforms: Dict[str, PlatformConfig] = field(default_factory=dict)
    search: SearchConfig = field(default_factory=SearchConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    drafting: DraftingConfig = field(default_factory=DraftingConfig)
    submission: SubmissionConfig = field(default_factory=SubmissionConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)

    # -- Dérivés utiles au reste du pipeline --------------------------------

    def enabled_platforms(self) -> List[str]:
        """Plateformes de scan activées, dans l'ordre de `KNOWN_PLATFORMS`."""
        return [
            key for key in KNOWN_PLATFORMS
            if self.platforms.get(key, PlatformConfig(enabled=False)).enabled
        ]

    def platform_config(self, source: str) -> PlatformConfig:
        return self.platforms.get(source, PlatformConfig())

    def language_for(self, source: str) -> str:
        override = self.platform_config(source).language
        return (override or self.profile.language or "fr").lower()

    def min_score_for(self, source: str) -> float:
        override = self.platform_config(source).min_score_for_draft
        return override if override is not None else self.scoring.min_score_for_draft

    def daily_quota_for(self, source: str) -> Optional[int]:
        return self.platform_config(source).daily_quota

    def candidate_profile(self, source: Optional[str] = None):
        """
        Construit un `job_scanner.scoring.profile.CandidateProfile` pour cette
        plateforme, en appliquant ses redéfinitions de taux/devise si elles
        existent (ex. Upwork en USD). Import différé pour éviter un cycle
        (`scoring/profile.py` ne connaît pas `config.py`).
        """
        from job_scanner.scoring.profile import CandidateProfile

        override = self.platform_config(source or "")
        return CandidateProfile(
            core_skills=dict(self.skills.core),
            exclusions=dict(self.skills.exclusions),
            reference_text=self.skills.reference_text or _default_reference_text(self.profile),
            target_rate=override.target_daily_rate or self.rates.target_daily_rate,
            floor_rate=override.floor_daily_rate or self.rates.floor_daily_rate,
            currency=override.currency or self.rates.currency,
            preferred_remote=tuple(self.conditions.preferred_remote),
            max_duration_months=self.conditions.max_duration_months,
        )

    def session_dir(self) -> Path:
        return Path(self.submission.session_dir).expanduser()


def _default_reference_text(profile: ProfileConfig) -> str:
    """Repli minimal si `skills.reference_text` n'est pas renseigné en config."""
    return f"{profile.name or 'Freelance'} — {profile.company or ''}".strip(" —")


def load_config(path: Optional[Path] = None) -> AppConfig:
    """
    Charge `config.yml`. Lève `ConfigError` s'il est absent : le pipeline ne
    doit jamais tourner sur un profil vide sans que l'utilisateur le sache.
    Lance `python3 onboard.py` pour le générer.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(
            f"Fichier de configuration introuvable : {config_path}\n"
            "Lance d'abord : python3 onboard.py"
        )

    with open(config_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    return _from_dict(raw)


def _from_dict(raw: Dict[str, Any]) -> AppConfig:
    profile = ProfileConfig(**_pick(raw.get("profile"), ProfileConfig))
    rates = RatesConfig(**_pick(raw.get("rates"), RatesConfig))
    skills = SkillsConfig(**_pick(raw.get("skills"), SkillsConfig))
    conditions = ConditionsConfig(**_pick(raw.get("conditions"), ConditionsConfig))
    search = SearchConfig(**_pick(raw.get("search"), SearchConfig))
    scoring = ScoringConfig(**_pick(raw.get("scoring"), ScoringConfig))
    drafting = DraftingConfig(**_pick(raw.get("drafting"), DraftingConfig))
    submission = SubmissionConfig(**_pick(raw.get("submission"), SubmissionConfig))
    notifications_raw = raw.get("notifications") or {}
    notifications = NotificationsConfig(
        console=notifications_raw.get("console", True),
        telegram_enabled=bool((notifications_raw.get("telegram") or {}).get("enabled", False)),
    )

    platforms: Dict[str, PlatformConfig] = {}
    for key in KNOWN_PLATFORMS:
        entry = (raw.get("platforms") or {}).get(key) or {}
        if not isinstance(entry, dict):
            entry = {}
        platforms[key] = PlatformConfig(**_pick(entry, PlatformConfig))

    return AppConfig(
        profile=profile, rates=rates, skills=skills, conditions=conditions,
        platforms=platforms, search=search, scoring=scoring, drafting=drafting,
        submission=submission, notifications=notifications,
    )


def _pick(raw: Optional[Dict[str, Any]], dataclass_type: Any) -> Dict[str, Any]:
    """Ne garde que les clés connues du dataclass : une config plus vieille que
    le code (nouveau champ ajouté) ne doit jamais faire planter le chargement."""
    if not raw:
        return {}
    known = {f.name for f in dataclass_type.__dataclass_fields__.values()}
    return {key: value for key, value in raw.items() if key in known}
