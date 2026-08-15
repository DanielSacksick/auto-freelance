"""
tests/test_draft.py — pure-function tests for the drafting quality gate.

`validate_draft`/`normalize_typography` (job_scanner/drafting/writer.py) are
plain string functions with no LLM call and no network I/O, so they're cheap
to test directly: this is what actually keeps a bad draft (too short, too
long, containing a banned phrase, or leaking an unfilled template) from ever
reaching a real application. draft.py / ApplicationWriter.write() are not
exercised here — they require a live LLM call, which is out of scope for an
offline test suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

writer = pytest.importorskip(
    "job_scanner.drafting.writer",
    reason="job_scanner/drafting/writer.py has not landed yet",
)

validate_draft = writer.validate_draft
normalize_typography = writer.normalize_typography
MIN_DRAFT_CHARS = writer.MIN_DRAFT_CHARS
MAX_DRAFT_CHARS = writer.MAX_DRAFT_CHARS


def _clean_fr_draft(target_len: int) -> str:
    """Builds a French draft inside the accepted length window, with none of
    the banned phrases/refusal markers/placeholders the gate rejects."""
    sentence = (
        "Le principal enjeu ne me semble pas la mise en place de l'outil, "
        "il s'agit surtout de fiabiliser le pipeline de bout en bout. "
    )
    body = "Bonjour,\n\n" + sentence * (target_len // len(sentence) + 1)
    body = body[: target_len - 20].rstrip() + "\n\nAlex Dupont"
    return body


def test_clean_draft_passes():
    body = _clean_fr_draft((MIN_DRAFT_CHARS + MAX_DRAFT_CHARS) // 2)
    assert MIN_DRAFT_CHARS <= len(body) <= MAX_DRAFT_CHARS
    assert validate_draft(body, "fr") is None


def test_too_short_draft_is_rejected():
    body = "Bonjour, ceci est un message beaucoup trop court pour être une candidature crédible."
    assert len(body) < MIN_DRAFT_CHARS
    problem = validate_draft(body, "fr")
    assert problem is not None
    assert "court" in problem


def test_too_long_draft_is_rejected():
    body = _clean_fr_draft(MAX_DRAFT_CHARS + 500)
    assert len(body) > MAX_DRAFT_CHARS
    problem = validate_draft(body, "fr")
    assert problem is not None
    assert "long" in problem


def test_banned_phrase_is_rejected():
    from job_scanner.drafting.prompts import pack_for

    banned = pack_for("fr").banned_phrases[0]
    body = _clean_fr_draft(MIN_DRAFT_CHARS + 100) + f" {banned}."
    problem = validate_draft(body, "fr")
    assert problem is not None
    assert "bannie" in problem


def test_refusal_marker_is_rejected():
    body = "En tant qu'IA, " + _clean_fr_draft(MIN_DRAFT_CHARS + 100)
    problem = validate_draft(body, "fr")
    assert problem is not None


def test_unfilled_placeholder_is_rejected():
    body = _clean_fr_draft(MIN_DRAFT_CHARS + 100) + " [NOM DU CLIENT]"
    problem = validate_draft(body, "fr")
    assert problem is not None
    assert "champ non rempli" in problem


def test_foreign_script_is_rejected():
    body = _clean_fr_draft(MIN_DRAFT_CHARS + 100) + " Спасибо"
    problem = validate_draft(body, "fr")
    assert problem is not None
    assert "alphabet" in problem


def test_english_banned_phrase_is_language_specific():
    # A French draft isn't penalized for containing an English cliche, and
    # vice versa: banned-phrase lists are per language, not global.
    body = _clean_fr_draft(MIN_DRAFT_CHARS + 100) + " perfect fit"
    assert validate_draft(body, "fr") is None


def test_normalize_typography_replaces_em_dash():
    result = normalize_typography("Un point important — à retenir.")
    assert "—" not in result
    assert "–" not in result


def test_normalize_typography_is_a_noop_on_clean_text():
    text = "Une phrase tout à fait normale, sans tiret cadratin."
    assert normalize_typography(text) == text
