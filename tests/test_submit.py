"""
tests/test_submit.py — platform-gating logic for submit.py.

submit.py is expected to expose `submit_offers(drafted, config, dry_run=False)`
and only attempt a real Playwright submission for offers whose platform is in
`config.AUTO_SUBMIT_PLATFORMS` (freework, freelance-informatique,
freelancermap — the only platforms with a `FormMapper`). Everything else
(upwork, linkedin) should come back with a manual/`None` submission marker
and must never reach `job_scanner.submission.playwright.submit()`.

As of this writing submit.py has not landed yet (only onboard.py,
config.example.yml, .env.example, config.py, scan.py, score.py, draft.py,
repo.py, and job_scanner/drafting/writer.py have). This test is written
against the documented spec so it starts passing as soon as submit.py lands;
until then `pytest.importorskip` turns the whole module into a single
skipped test rather than a collection error.

No real Playwright/browser is ever launched here: `job_scanner.submission.
playwright.submit` is monkeypatched to a recording stub, matching the
project's offline-testing convention (see job_scanner/submission/tests/).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as config_module  # noqa: E402
from job_scanner.models import RawOffer  # noqa: E402
from job_scanner.submission.playwright import SubmissionResult  # noqa: E402

submit_module = pytest.importorskip("submit", reason="submit.py has not landed yet")
submit_offers = submit_module.submit_offers


def _offer(source: str, external_id: str) -> RawOffer:
    return RawOffer(
        source=source,
        external_id=external_id,
        url=f"https://example.invalid/{source}/{external_id}",
        title=f"Mission {external_id}",
    )


def _drafted(source: str, external_id: str) -> Dict[str, Any]:
    return {
        "offer": _offer(source, external_id),
        "score": 85.0,
        "reasons": {},
        "draft_body": "Bonjour, " + ("texte de candidature. " * 40),
        "draft_model": "anthropic/claude-3.5-sonnet",
    }


@pytest.fixture()
def app_config():
    return config_module.AppConfig()


@pytest.fixture()
def fake_submit(monkeypatch):
    calls: List[Dict[str, Any]] = []

    def _fake(source: str, url: str, body: str, **kwargs: Any) -> SubmissionResult:
        calls.append({"source": source, "url": url, "body": body, **kwargs})
        return SubmissionResult(mode="auto", submitted=True, text="ok", url=url)

    # submit.py does `from job_scanner.submission.playwright import submit as
    # playwright_submit`, so the name that actually needs patching is the
    # local binding it kept, not the origin module's attribute (patching
    # job_scanner.submission.playwright.submit after the fact wouldn't touch
    # a reference submit.py already resolved at import time).
    monkeypatch.setattr(submit_module, "playwright_submit", _fake, raising=True)

    return calls


def test_auto_submit_platform_gets_a_real_submission_attempt(app_config, fake_submit):
    drafted = [_drafted("freework", "abc123")]

    results = submit_offers(drafted, app_config, dry_run=False)

    assert len(fake_submit) == 1
    assert fake_submit[0]["source"] == "freework"
    assert len(results) == 1
    assert results[0]["submission"] is not None


def test_non_auto_submit_platform_gets_no_submission_attempt(app_config, fake_submit):
    drafted = [_drafted("upwork", "xyz789")]

    results = submit_offers(drafted, app_config, dry_run=False)

    assert len(fake_submit) == 0
    assert len(results) == 1
    assert results[0]["submission"] is None


def test_mixed_batch_only_calls_submit_for_gated_platforms(app_config, fake_submit):
    drafted = [
        _drafted("freework", "a1"),
        _drafted("upwork", "a2"),
        _drafted("freelance-informatique", "a3"),
        _drafted("linkedin", "a4"),
    ]

    results = submit_offers(drafted, app_config, dry_run=False)

    called_sources = {call["source"] for call in fake_submit}
    assert called_sources == {"freework", "freelance-informatique"}
    assert len(results) == 4


def test_dry_run_passes_dry_run_flag_through(app_config, fake_submit):
    """--dry-run must not silently turn into a real send: the flag has to
    reach job_scanner.submission.playwright.submit(dry_run=True), which is
    what makes it fill the form without clicking submit."""
    drafted = [_drafted("freework", "a1")]

    submit_offers(drafted, app_config, dry_run=True)

    assert len(fake_submit) == 1
    assert fake_submit[0].get("dry_run") is True
