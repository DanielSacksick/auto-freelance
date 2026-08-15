#!/usr/bin/env python3
"""
onboard.py — Interactive setup for auto-freelance.

Walks a new user through building their personal `config.yml` (name, day
rate, skills, platforms to scan, ...) in about 5 minutes, then writes it in
the repo root using the exact schema `config.py` expects.

Stdlib only (input() prompts) — no third-party TUI dependency. PyYAML is
required to write the file (it's already a dependency of config.py itself).

Fields left as generic/empty defaults on purpose — hand-edit `config.yml`
afterwards for finer control:
  - skills.exclusions: no exclusion keywords are guessed; add your own
    (e.g. stacks or industries you want to avoid) directly in the YAML.
  - drafting.past_projects: left empty; add a few short, anonymized past
    project blurbs ({"domain": ..., "description": ...}) so cover letters
    can reference real work.
  - search.query: seeded from the core skills you enter here, but the
    search syntax/behavior is platform-specific — refine it by hand.

Run with: python3 onboard.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

try:
    import yaml
except ImportError:
    print(
        "PyYAML is required to write config.yml. Install it with:\n"
        "  pip install pyyaml\n"
        "(or activate the project's venv, if one exists)."
    )
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = REPO_ROOT / "config.yml"
EXAMPLE_PATH = REPO_ROOT / "config.example.yml"

KNOWN_PLATFORMS = (
    "freework",
    "freelance-informatique",
    "freelancermap",
    "upwork",
    "linkedin",
)
DEFAULT_ENABLED_PLATFORMS = {"freework", "freelance-informatique", "freelancermap"}
AUTO_SUBMIT_PLATFORMS = {"freework", "freelance-informatique", "freelancermap"}


# --------------------------------------------------------------------------
# Small input helpers (stdlib only)
# --------------------------------------------------------------------------

def ask(prompt: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw:
            if default:
                return default
            if not required:
                return ""
            print("  This field is required, please enter a value.")
            continue
        return raw


def ask_int(prompt: str, default: int) -> int:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print("  Please enter a whole number.")


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes", "o", "oui"):
            return True
        if raw in ("n", "no", "non"):
            return False
        print("  Please answer y or n.")


# --------------------------------------------------------------------------
# Onboarding flow
# --------------------------------------------------------------------------

def greet() -> None:
    print("=" * 70)
    print("auto-freelance — setup")
    print("=" * 70)
    print(
        "\nThis configures your personal auto-apply profile: name, day rate,\n"
        "skills, which job boards to scan. Takes about 5 minutes. You can\n"
        "hand-edit config.yml afterwards at any time, or re-run this script.\n"
    )


def check_existing_config() -> bool:
    """Returns True if we should proceed with writing config.yml."""
    if not CONFIG_PATH.exists():
        return True
    print(f"A config.yml already exists at {CONFIG_PATH}.")
    return ask_yes_no("Overwrite it?", default=False)


def ask_profile() -> Dict:
    print("\n-- Profile " + "-" * 58)
    name = ask("Your name", required=True)
    company = ask("Company / brand name (optional, press Enter to skip)")
    email = ask("Email", required=True)
    linkedin_url = ask("LinkedIn URL")
    portfolio_url = ask("Portfolio URL (optional)")
    github_url = ask("GitHub URL (optional)")
    cv_path = ask("Path to your CV (PDF)", required=True)
    resolved_cv = Path(cv_path).expanduser()
    if not resolved_cv.is_file():
        print(f"  Warning: '{cv_path}' doesn't exist yet — you can add it later.")
    language = ask("Preferred language for scanning/drafting (fr/en)", default="fr").lower()
    if language not in ("fr", "en"):
        print("  Unrecognized language, defaulting to 'fr'.")
        language = "fr"
    return {
        "name": name,
        "company": company,
        "email": email,
        "portfolio_url": portfolio_url,
        "linkedin_url": linkedin_url,
        "github_url": github_url,
        "cv_path": cv_path,
        "language": language,
    }


def ask_rates() -> Dict:
    print("\n-- Day rate (TJM) " + "-" * 51)
    currency = ask("Currency", default="EUR").upper()
    floor_rate = ask_int("Floor day rate (minimum you'll accept)", default=400)
    target_rate = ask_int("Target day rate", default=max(floor_rate, 600))
    if target_rate < floor_rate:
        print("  Target is below floor — using floor as target too.")
        target_rate = floor_rate
    return {
        "target_daily_rate": target_rate,
        "floor_daily_rate": floor_rate,
        "currency": currency,
    }


def ask_skills() -> Dict:
    print("\n-- Skills " + "-" * 59)
    print(
        "Enter your core skills/keywords, comma-separated (e.g. Python,\n"
        "FastAPI, PostgreSQL, Docker). Each gets a default weight of 1.0 —\n"
        "you can fine-tune weights by hand in config.yml afterwards."
    )
    raw = ask("Core skills", required=True)
    core = {term.strip(): 1.0 for term in raw.split(",") if term.strip()}
    return core


def ask_platforms() -> Dict[str, Dict]:
    print("\n-- Platforms to scan " + "-" * 48)
    print(
        "Select which job boards to scan. freework, freelance-informatique\n"
        "and freelancermap support automatic submission; upwork and linkedin\n"
        "are scan/score/draft only (manual apply — Upwork spends paid Connects)."
    )
    platforms: Dict[str, Dict] = {}
    for key in KNOWN_PLATFORMS:
        default = key in DEFAULT_ENABLED_PLATFORMS
        note = "" if key in AUTO_SUBMIT_PLATFORMS else " (manual apply only)"
        enabled = ask_yes_no(f"Enable {key}{note}?", default=default)
        platforms[key] = {"enabled": enabled}
    return platforms


def ask_drafting(core_skills: Dict[str, float]) -> Dict:
    print("\n-- Cover letter drafting " + "-" * 44)
    bio = ask(
        "Short bio for cover letters (one paragraph, press Enter when done)",
        required=True,
    )
    return {
        "bio": bio,
        # Left empty on purpose — add real anonymized past projects by hand,
        # see module docstring.
        "past_projects": [],
        "model": "anthropic/claude-3.5-sonnet",
        "temperature": 0.6,
    }


def ask_notifications() -> Dict:
    print("\n-- Notifications " + "-" * 52)
    telegram_enabled = ask_yes_no("Enable Telegram notifications?", default=False)
    if telegram_enabled:
        print(
            "  Remember to set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your\n"
            "  .env file (see .env.example) — never store tokens in config.yml."
        )
    return {
        "console": True,
        "telegram": {"enabled": telegram_enabled},
    }


def build_search(core_skills: Dict[str, float], floor_rate: int) -> Dict:
    if core_skills:
        query = " OR ".join(core_skills.keys())
    else:
        query = "freelance OR mission"
    return {
        "query": query,
        "min_daily_rate": floor_rate,
        "max_pages": 2,
    }


def build_config(
    profile: Dict,
    rates: Dict,
    core_skills: Dict[str, float],
    platforms: Dict[str, Dict],
    drafting: Dict,
    notifications: Dict,
) -> Dict:
    return {
        "profile": profile,
        "rates": rates,
        "skills": {
            "core": core_skills,
            # Left empty on purpose — add exclusion keywords by hand, see
            # module docstring.
            "exclusions": {},
            "reference_text": "",
        },
        "conditions": {
            "preferred_remote": ["full", "partial"],
            "max_duration_months": 18,
        },
        "platforms": platforms,
        "search": build_search(core_skills, rates["floor_daily_rate"]),
        "scoring": {"min_score_for_draft": 60.0},
        "drafting": drafting,
        "submission": {
            "headless": True,
            "session_dir": "~/.auto-freelance/sessions",
        },
        "notifications": notifications,
    }


def write_config(data: Dict) -> None:
    header = (
        "# config.yml — generated by onboard.py.\n"
        "# Personal data: not committed to git (see .gitignore).\n"
        "# Hand-edit freely — see config.example.yml for field-by-field docs,\n"
        "# in particular skills.exclusions, drafting.past_projects and\n"
        "# search.query, which onboard.py leaves as generic defaults.\n\n"
    )
    body = yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    CONFIG_PATH.write_text(header + body, encoding="utf-8")
    print(f"\nWrote {CONFIG_PATH}")


def run_sanity_check() -> None:
    if not ask_yes_no("\nRun a quick sanity check on config.yml now?", default=True):
        return
    sys.path.insert(0, str(REPO_ROOT))
    try:
        import config as config_module
    except ImportError as exc:
        print(f"  Could not import config.py: {exc}")
        return

    try:
        app_config = config_module.load_config(CONFIG_PATH)
    except config_module.ConfigError as exc:
        print(f"  config.yml failed to load: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - surface any parse/schema issue to the user
        print(f"  Unexpected error loading config.yml: {exc}")
        return

    enabled = app_config.enabled_platforms()
    print("\nconfig.yml loaded successfully:")
    print(f"  Name:        {app_config.profile.name or '(not set)'}")
    print(
        f"  Day rate:    {app_config.rates.floor_daily_rate}"
        f"-{app_config.rates.target_daily_rate} {app_config.rates.currency}"
    )
    print(f"  Platforms:   {', '.join(enabled) if enabled else '(none enabled)'}")
    print(f"  Skills:      {len(app_config.skills.core)} core keyword(s)")
    print("\nYou're set. Next: run the scanner (see README) to fetch and score offers.")


def main() -> None:
    greet()
    if not check_existing_config():
        print("Aborted — config.yml left unchanged.")
        return

    profile = ask_profile()
    rates = ask_rates()
    core_skills = ask_skills()
    platforms = ask_platforms()
    drafting = ask_drafting(core_skills)
    notifications = ask_notifications()

    config_data = build_config(profile, rates, core_skills, platforms, drafting, notifications)
    write_config(config_data)
    run_sanity_check()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)
