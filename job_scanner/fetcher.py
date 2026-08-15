"""
http.py — Transport HTTP des sources d'opportunités.

Isolé du parsing pour deux raisons : les tests de parsing tournent hors-ligne,
et une source peut être servie par HTTP, par un cache ou par un navigateur
piloté sans toucher à son code.

Politesse volontaire : un délai entre les pages, un User-Agent honnête, un
timeout borné. On lit des annonces publiques à raison d'une poignée de requêtes
par jour — il n'y a aucune raison de se comporter autrement.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20
DEFAULT_DELAY_SECONDS = 1.5

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


class HttpFetcher:
    """Fetcher réutilisable : garde la session ouverte et espace les appels."""

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        session: Optional[requests.Session] = None,
    ):
        self._timeout = timeout
        self._delay = delay_seconds
        self._session = session or requests.Session()
        self._session.headers.update(_HEADERS)
        self._last_call: float = 0.0

    def __call__(self, url: str) -> str:
        self._wait_turn()
        logger.debug("GET %s", url)
        response = self._session.get(url, timeout=self._timeout)
        response.raise_for_status()
        self._last_call = time.monotonic()
        return response.text

    def _wait_turn(self) -> None:
        """Respecte le délai minimal entre deux requêtes."""
        if not self._last_call:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
