"""
tests/test_notify_submissions.py — notify.notify_submission_results().

A submission that's clicked but not confirmed (SubmissionResult.submitted is
False, e.g. mode="error", reason="no_success_proof") used to be visible only
in the run logs — never in the Telegram report Dan actually reads. This
covers that both confirmed and unconfirmed attempts reach the report, with
the unconfirmed ones carrying their reason, and that offers never attempted
(submission=None) are excluded entirely.

No real Telegram call is made: job_scanner.telegram_sender.send_text is
monkeypatched to a recording stub, matching the project's offline-testing
convention (see tests/test_submit.py).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from job_scanner.models import RawOffer  # noqa: E402
from job_scanner.submission.playwright import SubmissionResult  # noqa: E402
from notify import notify_submission_results  # noqa: E402


def _offer(title: str) -> RawOffer:
    return RawOffer(source="freework", external_id=title, url="https://example.invalid", title=title)


class _FakeNotifications:
    def __init__(self, telegram_enabled: bool = True):
        self.telegram_enabled = telegram_enabled


class _FakeConfig:
    def __init__(self, telegram_enabled: bool = True):
        self.notifications = _FakeNotifications(telegram_enabled)


@pytest.fixture()
def fake_send_text(monkeypatch):
    calls: List[Dict[str, Any]] = []

    def _fake(token: str, chat_id: str, text: str):
        calls.append({"token": token, "chat_id": chat_id, "text": text})
        return 1

    monkeypatch.setattr("notify.telegram_sender.send_text", _fake, raising=True)
    return calls


@pytest.fixture(autouse=True)
def telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")


def test_confirmed_and_unconfirmed_both_reach_the_report(fake_send_text):
    submitted = [
        {
            "offer": _offer("Mission confirmée"),
            "submission": SubmissionResult(mode="auto", submitted=True, text="✅ Envoyée"),
        },
        {
            "offer": _offer("TECH LEAD PYTHON / ANGULAR"),
            "submission": SubmissionResult(
                mode="error", submitted=False,
                text="⚠️ Envoi cliqué mais aucune confirmation observée.",
            ),
        },
    ]

    report = notify_submission_results(_FakeConfig(), submitted)

    assert report == {"sent": True, "confirmed": 1, "unconfirmed": 1}
    assert len(fake_send_text) == 1
    text = fake_send_text[0]["text"]
    assert "Mission confirmée" in text
    assert "TECH LEAD PYTHON / ANGULAR" in text
    assert "aucune confirmation observée" in text


def test_offers_never_attempted_are_excluded(fake_send_text):
    submitted = [{"offer": _offer("Upwork, jamais tentée"), "submission": None}]

    report = notify_submission_results(_FakeConfig(), submitted)

    assert report == {"sent": False, "confirmed": 0, "unconfirmed": 0}
    assert fake_send_text == []


def test_no_attempts_at_all_sends_nothing(fake_send_text):
    report = notify_submission_results(_FakeConfig(), [])

    assert report == {"sent": False, "confirmed": 0, "unconfirmed": 0}
    assert fake_send_text == []


def test_telegram_disabled_sends_nothing(fake_send_text):
    submitted = [{
        "offer": _offer("Mission"),
        "submission": SubmissionResult(mode="auto", submitted=True, text="ok"),
    }]

    report = notify_submission_results(_FakeConfig(telegram_enabled=False), submitted)

    assert report["sent"] is False
    assert fake_send_text == []
