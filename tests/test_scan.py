"""
tests/test_scan.py — offline parsing tests for job_scanner/sources/*.

No fixture from job_scanner/submission/tests/fixtures/ fits here: those HTML
files (freework.html, fi_mission.html, freelancermap.html) are single-offer
*application-form* pages used by the Playwright submission tests, not the
multi-offer *search-result* pages the scan sources parse. There is no
sources-level test directory in the original project this repo was extracted
from either (confirmed: no test_*source* / *sources*test* files under
job_scanner/). So each source below gets its own small inline fixture built
to the real shape its parser expects (verified by reading the source file),
with a fake `Fetcher` closure standing in for HTTP.

freework and freelance-informatique get full parsing assertions since their
formats are simple enough to construct exactly (a decoded devalue payload,
and static HTML with the expected CSS hooks). freelancermap gets a full
assertion too (its React-component JSON payload is likewise reproducible).
linkedin and upwork are deliberately smoke-tested at construction time only:
LinkedInJobsSource.fetch() always calls out to Jina Reader over a real
`urllib.request` regardless of the injected fetcher, and UpworkSource talks
GraphQL through a client that needs real credentials — actually invoking
either .fetch() here would mean a real network call, which the project's own
testing convention (see CLAUDE.md / job_scanner/submission/tests/) forbids.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, List

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from job_scanner.sources.base import SearchCriteria  # noqa: E402
from job_scanner.sources.freework import FreeWorkSource  # noqa: E402
from job_scanner.sources.freelance_informatique import (  # noqa: E402
    FreelanceInfoConfig,
    FreelanceInformatiqueSource,
)
from job_scanner.sources.freelancermap import (  # noqa: E402
    FreelancermapConfig,
    FreelancermapSource,
)
from job_scanner.sources.linkedin import LinkedInJobsSource  # noqa: E402
from job_scanner.sources.upwork import UpworkSource  # noqa: E402


# ---------------------------------------------------------------------------
# Tiny devalue encoder — the inverse of job_scanner/devalue.py::parse_devalue,
# just enough to build a valid __NUXT_DATA__ payload from a plain Python
# object for tests. No value sharing/memoization (not needed for a fixture),
# but the format devalue.py decodes doesn't require it to be correct.
# ---------------------------------------------------------------------------


def _encode_devalue(value: Any, flat: List[Any]) -> int:
    if isinstance(value, dict):
        index = len(flat)
        flat.append(None)
        obj = {key: _encode_devalue(val, flat) for key, val in value.items()}
        flat[index] = obj
        return index
    if isinstance(value, list):
        index = len(flat)
        flat.append(None)
        arr = [_encode_devalue(item, flat) for item in value]
        flat[index] = arr
        return index
    index = len(flat)
    flat.append(value)
    return index


def _nuxt_html(payload: Any) -> str:
    flat: List[Any] = []
    _encode_devalue(payload, flat)
    return (
        "<!DOCTYPE html><html><body>"
        f'<script id="__NUXT_DATA__" type="application/json">{json.dumps(flat)}</script>'
        "</body></html>"
    )


# -- FreeWork -----------------------------------------------------------------


def test_freework_parses_a_real_shaped_payload():
    job = {
        "id": "job-12345",
        "slug": "architecte-ia",
        "title": "Architecte IA",
        "dailySalary": "500-550 €",
        "company": {"name": "Acme Corp"},
        "location": {"label": "Paris"},
        "description": "<p>Mission de conception d'agents LLM.</p>",
        "publishedAt": "2026-08-01T00:00:00",
        "durationValue": 6,
        "durationPeriod": "month",
        "remoteMode": "full",
        "jobPostingType": "contractor",
        "skills": [{"name": "Python"}, {"name": "LLM"}],
        "cardProps": {"to": "/fr/tech-it/job-mission/dev/architecte-ia"},
    }
    payload = {"jobs": [job], "totalItems": 1}
    html = _nuxt_html(payload)

    source = FreeWorkSource(fetcher=lambda url: html)
    offers = source.fetch(SearchCriteria(query="ia", max_pages=1))

    assert len(offers) == 1
    offer = offers[0]
    assert offer.source == "freework"
    assert offer.external_id == "job-12345"
    assert offer.title == "Architecte IA"
    assert offer.company == "Acme Corp"
    assert offer.daily_rate_min == 500 and offer.daily_rate_max == 550
    assert offer.duration_months == 6
    assert offer.skills == ["Python", "LLM"]
    assert offer.url.endswith("/fr/tech-it/job-mission/dev/architecte-ia")


def test_freework_missing_nuxt_script_returns_empty_list_without_raising():
    source = FreeWorkSource(fetcher=lambda url: "<html><body>no data here</body></html>")
    offers = source.fetch(SearchCriteria(query="ia", max_pages=1))
    assert offers == []


# -- Freelance-Informatique -----------------------------------------------


FI_LISTING_HTML = """
<!DOCTYPE html>
<html lang="fr"><body>
<main>
  <p>Offres 1 à 1 sur 1 résultats</p>
  <div class="job-card-line">
    <h2 class="job-title"><a href="/mission-consultant-ia-paris-260801C001">Consultant IA Paris</a></h2>
    <p>Mission de conseil en intelligence artificielle pour un grand compte.</p>
    <ul>
      <li><i class="icon-clock"></i> Publiée le 01/08</li>
      <li><i class="icon-map"></i> Paris</li>
      <li><i class="icon-time"></i> 3 mois</li>
    </ul>
  </div>
</main>
</body></html>
"""


def test_freelance_informatique_parses_a_listing_card():
    config = FreelanceInfoConfig(terms=("intelligence artificielle",), page_size=50)
    source = FreelanceInformatiqueSource(fetcher=lambda url: FI_LISTING_HTML, config=config)

    offers = source.fetch(SearchCriteria(query="ia", max_pages=1))

    assert len(offers) == 1
    offer = offers[0]
    assert offer.source == "freelance-informatique"
    assert offer.external_id == "260801C001"
    assert offer.title == "Consultant IA Paris"
    assert offer.location == "Paris"
    assert offer.duration_months == 3
    # TJM is deliberately never published on this platform (hidden behind
    # "Voir le tarif") — the source must not invent a value.
    assert offer.daily_rate_min is None and offer.daily_rate_max is None


def test_freelance_informatique_no_counter_returns_empty_list_without_raising():
    config = FreelanceInfoConfig(terms=("cobol",), page_size=50)
    html = "<html><body><main>Aucun résultat filtré ici.</main></body></html>"
    source = FreelanceInformatiqueSource(fetcher=lambda url: html, config=config)

    offers = source.fetch(SearchCriteria(query="cobol", max_pages=1))
    assert offers == []


# -- freelancermap ----------------------------------------------------------


def _freelancermap_html(term: str, projects: List[dict]) -> str:
    payload = {
        "initialState": {"filter": {"query": term}},
        "initialResults": projects,
    }
    body = json.dumps(payload)
    return (
        '<html><body><script class="js-react-on-rails-component" '
        'data-component-name="ProjectSearch">' + body + "</script></body></html>"
    )


def test_freelancermap_parses_a_real_shaped_payload():
    project = {
        "id": 987654,
        "title": "Data Engineer IA",
        "company": "Acme SAS",
        "description": "Construction d'un pipeline de données pour un projet IA.",
        "created": "2026-08-01T00:00:00+02:00",
        "duration": 4,
        "projectContractType": {"type": "contracting", "remoteInPercent": 100},
        "city": "Lyon",
        "country": {"name": "France"},
        "skills": [{"name": "Python"}, {"name": "Airflow"}],
        "links": {"project": "/project/data-engineer-ia-987654"},
    }
    html = _freelancermap_html("intelligence artificielle", [project])
    config = FreelancermapConfig(
        terms=("intelligence artificielle",), contract_types=("contracting",), page_size=22
    )
    source = FreelancermapSource(fetcher=lambda url: html, config=config)

    offers = source.fetch(SearchCriteria(query="ia", max_pages=1))

    assert len(offers) == 1
    offer = offers[0]
    assert offer.source == "freelancermap"
    assert offer.external_id == "987654"
    assert offer.title == "Data Engineer IA"
    assert offer.remote_mode == "full"
    assert offer.location == "Lyon, France"
    assert offer.skills == ["Python", "Airflow"]


def test_freelancermap_query_not_applied_returns_empty_list_without_raising():
    # The site echoes back a different filter.query than what was asked —
    # the source must refuse the page rather than return unrelated results.
    html = _freelancermap_html("cobol", [{"id": 1, "title": "x"}])
    config = FreelancermapConfig(terms=("intelligence artificielle",), page_size=22)
    source = FreelancermapSource(fetcher=lambda url: html, config=config)

    offers = source.fetch(SearchCriteria(query="ia", max_pages=1))
    assert offers == []


# -- LinkedIn / Upwork: construction-only smoke tests ------------------------
#
# Both sources bypass the injected Fetcher for their real transport (Jina
# Reader over urllib for LinkedIn, an authenticated GraphQL client for
# Upwork) — calling .fetch() here would mean a real network call, which is
# out of bounds for this offline suite. We only check that constructing the
# source (as scan.py does) doesn't require network access or raise.


def test_linkedin_source_is_constructible_without_network():
    source = LinkedInJobsSource()
    assert source.key == "linkedin"


def test_upwork_source_is_constructible_without_a_live_client():
    # client=None is the documented default: a real GraphQL client is built
    # lazily on first scan, never at construction time, precisely so that
    # Upwork being enabled in config.yml can't block other platforms or
    # require credentials just to instantiate the source.
    source = UpworkSource(client=None)
    assert source.key == "upwork"
