"""
tests/test_config.py — config.example.yml <-> config.py schema round-trip.

This is a regression test in the strict sense: config.example.yml is the
public reference every user copies from, and config.py is the schema that
consumes it. If either drifts from the other (a field renamed in one but not
the other, a type changed), onboard.py's output — and every real user's
config.yml — silently breaks. This test catches that by loading the example
file through the real `load_config()` and asserting the result looks sane.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import AppConfig, load_config  # noqa: E402
from job_scanner.scoring.profile import CandidateProfile  # noqa: E402

EXAMPLE_CONFIG = ROOT / "config.example.yml"


@pytest.fixture(scope="module")
def config() -> AppConfig:
    if not EXAMPLE_CONFIG.is_file():
        pytest.skip("config.example.yml not landed yet")
    return load_config(path=EXAMPLE_CONFIG)


def test_loads_as_appconfig(config: AppConfig):
    assert isinstance(config, AppConfig)


def test_profile_name_is_a_non_empty_string(config: AppConfig):
    assert isinstance(config.profile.name, str)
    assert config.profile.name.strip() != ""


def test_profile_language_is_a_known_code(config: AppConfig):
    assert config.profile.language.lower() in ("fr", "en")


def test_rates_target_above_floor(config: AppConfig):
    assert isinstance(config.rates.target_daily_rate, int)
    assert isinstance(config.rates.floor_daily_rate, int)
    assert config.rates.target_daily_rate > config.rates.floor_daily_rate > 0


def test_enabled_platforms_returns_a_list(config: AppConfig):
    platforms = config.enabled_platforms()
    assert isinstance(platforms, list)
    assert len(platforms) > 0
    # freework/freelance-informatique/freelancermap ship enabled by default
    # in the example file; a platform is enabled by explicit opt-in.
    assert "freework" in platforms


def test_candidate_profile_carries_the_configured_rate(config: AppConfig):
    profile = config.candidate_profile()
    assert isinstance(profile, CandidateProfile)
    assert profile.target_rate == config.rates.target_daily_rate
    assert profile.floor_rate == config.rates.floor_daily_rate
    assert profile.currency == config.rates.currency


def test_candidate_profile_applies_per_platform_override(config: AppConfig):
    # config.example.yml overrides Upwork to USD with its own rate/floor —
    # this is the one field most likely to silently drift if the per-platform
    # override plumbing in AppConfig.candidate_profile() ever breaks.
    upwork_cfg = config.platforms.get("upwork")
    if upwork_cfg is None or upwork_cfg.currency is None:
        pytest.skip("config.example.yml has no Upwork currency override to check")
    profile = config.candidate_profile("upwork")
    assert profile.currency == upwork_cfg.currency
    assert profile.target_rate == upwork_cfg.target_daily_rate


def test_skills_core_is_a_weight_dict(config: AppConfig):
    assert isinstance(config.skills.core, dict)
    assert len(config.skills.core) > 0
    for weight in config.skills.core.values():
        assert isinstance(weight, (int, float))
        assert 0 <= weight <= 1


def test_min_score_for_has_a_sane_default(config: AppConfig):
    threshold = config.min_score_for("freework")
    assert isinstance(threshold, float)
    assert 0 <= threshold <= 100
