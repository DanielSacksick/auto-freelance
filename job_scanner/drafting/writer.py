"""
writer.py — Drafts the cover letter for one offer.

A generic cover letter gets ~2% reply rate. What works: showing in three lines
that you read the post and have already delivered the same thing elsewhere.
The writer therefore receives the offer, the skills that triggered the match,
and the candidate's real background (from `config.yml`) — then produces a
short, situated message.

Security: an offer's description is **untrusted external content**. A post
could contain "ignore the previous instructions". The text is therefore
wrapped in explicit delimiters and the system prompt requires that its
content never be treated as an instruction — see `prompts.py`.

The drafting language depends on the platform (`config.language_for(source)`);
the instructions and quality guards for each language live in `prompts.py`,
this module only applies them.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import requests

from job_scanner.drafting.prompts import (
    DEFAULT_LANGUAGE,
    LanguagePack,
    pack_for,
    system_prompt_for,
)
from job_scanner.models import RawOffer

logger = logging.getLogger(__name__)

# A LLM client takes (system, user) and returns the produced text.
LlmCaller = Callable[[str, str], str]

MAX_DESCRIPTION_CHARS = 4000
MIN_DRAFT_CHARS = 900
# A read of the real stake, a position and a grounding in experience all need
# room. Below 900 characters the text turns back into a generic application;
# beyond 2400 it stops being read.
MAX_DRAFT_CHARS = 2400

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-3.5-sonnet"
DEFAULT_TEMPERATURE = 0.6
DEFAULT_MAX_TOKENS = 1200
DEFAULT_TIMEOUT = 60

# Cyrillic, Greek, CJK, Arabic, Hebrew: a sign the model derailed mid-sentence.
_FOREIGN_SCRIPT_RE = re.compile(
    r"[Ѐ-ӿͰ-Ͽ一-鿿぀-ヿ؀-ۿ֐-׿]"
)

# Unfilled templates: {{PROJECT}}, [RESULT], <AVAILABILITY>...
_PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}|\[[A-ZÀ-Ÿ][A-ZÀ-Ÿ _'-]{2,}\]|<[A-ZÀ-Ÿ][A-ZÀ-Ÿ _'-]{2,}>")

# Em dashes and en dashes: a typographic tell of generated text. Normalized
# rather than rejected — losing a good application over a dash would be
# absurd, and the replacement is unambiguous.
_DASH_RE = re.compile(r"\s*[—–]\s*")


@dataclass(frozen=True)
class Draft:
    """A cover letter ready for review."""

    body: str
    model: Optional[str] = None


class ApplicationWriter:
    """Produces the text of a cover letter from an offer."""

    def __init__(
        self,
        llm: LlmCaller,
        name: str = "",
        company: str = "",
        portfolio_url: str = "",
        bio: str = "",
        past_projects: Optional[List[Dict[str, str]]] = None,
        model_label: Optional[str] = None,
        language: str = DEFAULT_LANGUAGE,
    ):
        """
        Args:
            name, company, portfolio_url, bio, past_projects: the
                candidate's own identity and background, normally built from
                `AppConfig.profile` / `AppConfig.drafting` (see `draft.py`).
                Never hardcoded here.
            language: drafting language. French remains the default so that
                omitting it doesn't silently change behaviour.
        """
        self._llm = llm
        self._name = name
        self._company = company
        self._portfolio_url = portfolio_url
        self._bio = bio
        self._past_projects = past_projects or []
        self._model_label = model_label
        self.language = (language or DEFAULT_LANGUAGE).lower()
        self._pack: LanguagePack = pack_for(self.language)
        self._system_prompt = system_prompt_for(
            self.language,
            name=self._name,
            company=self._company,
            portfolio_url=self._portfolio_url,
            bio=self._bio,
            past_projects=self._past_projects,
        )

    def write(self, offer: RawOffer, matched_skills: Optional[List[str]] = None) -> Optional[Draft]:
        """
        Drafts the cover letter. Returns None if the model fails — the
        caller then decides to retry later rather than send an empty draft.
        """
        prompt = self._build_user_prompt(offer, matched_skills)
        cleaned = self._attempt(prompt, offer.title)
        if cleaned is None:
            return None

        problem = validate_draft(cleaned, self.language)
        if problem is None:
            return Draft(body=cleaned, model=self._model_label)

        # A length overrun is worth a second pass: better a second attempt
        # than no draft at all. Other reasons (leak, empty field) are not
        # negotiable — no draft beats a questionable one.
        if not problem.startswith("trop long"):
            logger.warning("drafting: rejected on \"%s\" — %s", offer.title, problem)
            return None

        logger.info("drafting: \"%s\" %s, second pass", offer.title, problem)
        retry = self._attempt(
            prompt
            + "\n\n"
            + self._pack.retry_template.format(
                length=len(cleaned), budget=MAX_DRAFT_CHARS - 500
            ),
            offer.title,
        )
        if retry is None:
            return None

        problem = validate_draft(retry, self.language)
        if problem:
            logger.warning("drafting: rejected after second pass on \"%s\" — %s", offer.title, problem)
            return None

        return Draft(body=retry, model=self._model_label)

    def _attempt(self, prompt: str, title: str) -> Optional[str]:
        """One model call, typography normalized; None if the transport fails."""
        try:
            raw = self._llm(self._system_prompt, prompt) or ""
        except Exception as exc:
            logger.warning("drafting: failed on \"%s\" (%s)", title, exc)
            return None
        return normalize_typography(raw.strip()).strip()

    def _build_user_prompt(self, offer: RawOffer, matched_skills: Optional[List[str]]) -> str:
        description = (offer.description or "").strip()[:MAX_DESCRIPTION_CHARS]
        pack, labels = self._pack, self._pack.labels

        facts = [f"{labels['title']} : {offer.title}"]
        if offer.company:
            facts.append(f"{labels['company']} : {offer.company}")
        if offer.location:
            facts.append(f"{labels['location']} : {offer.location}")
        if offer.duration_months:
            facts.append(f"{labels['duration']} : {offer.duration_months} {labels['duration_unit']}")
        if offer.remote_mode:
            facts.append(f"{labels['remote']} : {offer.remote_mode}")
        if offer.skills:
            facts.append(f"{labels['skills']} : {', '.join(offer.skills[:15])}")
        if matched_skills:
            facts.append(f"{pack.hooks_label} : " + ", ".join(matched_skills[:8]))

        return (
            f"{pack.intro}\n\n"
            + "\n".join(facts)
            + f"\n\n<{pack.job_tag}>\n{description}\n</{pack.job_tag}>\n\n"
            + pack.portfolio_line.format(url=self._portfolio_url)
        )


def normalize_typography(body: str) -> str:
    """
    Replaces em dashes with ordinary punctuation.

    An em dash mid-sentence is one of the most recognizable markers of
    generated text. We pick a comma, which fits most asides, and a colon
    when the dash introduced an enumeration at the end of a sentence.
    """
    if not body:
        return body

    def replace(match: "re.Match") -> str:
        start, end = match.span()
        before, after = body[:start], body[end:]
        # A dash followed by an enumeration or a final explanation reads
        # better as a colon; elsewhere, a comma is enough.
        if before.rstrip().endswith((":", ",")):
            return " "
        return " : " if after and "," in after[:80] and after[:1].islower() else ", "

    cleaned = _DASH_RE.sub(replace, body)
    # A comma added just before strong punctuation is superfluous.
    return re.sub(r",\s*([.!?;:])", r"\1", cleaned)


def validate_draft(body: str, language: str = DEFAULT_LANGUAGE) -> Optional[str]:
    """
    Quality gate before a text goes out under the candidate's name.

    Returns the rejection reason, or None if the draft is acceptable. These
    guards come from failures observed in production: a pooled model once
    inserted a Cyrillic word mid-sentence in a French draft.

    Lengths, alphabets and unfilled templates apply to every language. Banned
    phrases, on the other hand, are language-specific: "je reste à votre
    disposition" and "looking forward to hearing from you" are the same flaw
    in two languages, and no single list covers both.
    """
    pack = pack_for(language)
    if len(body) < MIN_DRAFT_CHARS:
        return f"trop court ({len(body)} caractères)"
    if len(body) > MAX_DRAFT_CHARS:
        return f"trop long ({len(body)} caractères)"

    stray = _FOREIGN_SCRIPT_RE.findall(body)
    if stray:
        return f"caractères hors alphabet latin : {''.join(stray[:12])}"

    # An unfilled field kills the application.
    placeholder = _PLACEHOLDER_RE.search(body)
    if placeholder:
        return f"champ non rempli : {placeholder.group(0)[:40]}"

    lowered = body.lower()
    for marker in pack.refusal_markers:
        if marker in lowered:
            return "le modèle a répondu au lieu de rédiger"
    for cliche in pack.banned_phrases:
        if cliche in lowered:
            return f"formule bannie : « {cliche} »"
    return None


def default_llm_caller(
    model: Optional[str] = DEFAULT_MODEL, temperature: float = DEFAULT_TEMPERATURE
) -> LlmCaller:
    """
    Wires the writer to OpenRouter's chat completions API directly.

    Requires `OPENROUTER_API_KEY` in the environment (loaded from `.env` once
    at the CLI entrypoint, see `draft.py`/`pipeline.py` — not re-loaded here).
    Raises a clear `RuntimeError` if the key is missing or the call fails,
    rather than crashing with a bare traceback deep inside the writer.
    """

    def call(system: str, user: str) -> str:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set. Add it to your .env "
                "(see .env.example) to enable cover-letter drafting."
            )

        payload = {
            "model": model or DEFAULT_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                OPENROUTER_URL, json=payload, headers=headers, timeout=DEFAULT_TIMEOUT
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"OpenRouter call failed: {exc}") from exc

        data = response.json()
        return _extract_text(data)

    return call


def _extract_text(data: Dict[str, Any]) -> str:
    """Extracts the message content from an OpenRouter/OpenAI-shaped response."""
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected OpenRouter response shape: {data!r}") from exc
