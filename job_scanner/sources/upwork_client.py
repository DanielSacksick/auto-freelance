"""
upwork_client.py — Accès à l'API GraphQL officielle d'Upwork.

Upwork n'expose pas ses annonces dans une page lisible : il faut passer par
`https://api.upwork.com/graphql`, en OAuth2. C'est une bonne nouvelle — aucune
automatisation de navigateur, donc aucun risque pour le compte.

Ce module ne connaît rien aux missions : il sait ouvrir une session, garder le
jeton frais et exécuter une requête. Le vocabulaire métier vit dans `upwork.py`.

Le transport est **injecté** comme ailleurs dans ce paquet : les tests tournent
hors-ligne et le client reste utilisable derrière un proxy ou un cache.

Points de vigilance OAuth2 :

- Le flux est `authorization_code`. Le consentement se fait **une fois à la
  main** (`authorization_url` puis `exchange_code`) ; ensuite le cron ne fait
  que rafraîchir.
- L'access token vit 24 h, le refresh token le renouvelle. Upwork ne renvoie
  pas toujours un nouveau refresh token : on conserve l'ancien.
- Le jeton rafraîchi doit être **repersisté**, sinon le cron redemande un
  consentement à chaque réveil.

Limites de l'API, mesurées côté Upwork : 10 requêtes/seconde par IP et 40 000
par jour. Un scan quotidien en consomme une poignée ; le délai de politesse est
là par principe, pas par nécessité.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

BASE_HOST = "https://www.upwork.com"
GRAPHQL_URL = "https://api.upwork.com/graphql"
TOKEN_URL = f"{BASE_HOST}/api/v3/oauth2/token"
AUTHORIZE_URL = f"{BASE_HOST}/ab/account-security/oauth2/authorize"

DEFAULT_TIMEOUT = 30
DEFAULT_DELAY_SECONDS = 1.0

# Marge avant expiration : on rafraîchit un peu en avance plutôt que de
# découvrir l'expiration au milieu d'un scan.
EXPIRY_SKEW_SECONDS = 120

DEFAULT_TOKEN_PATH = Path.home() / ".auto-freelance" / "credentials" / "upwork_token.json"

#: (url, json=…, data=…, headers=…) -> (statut HTTP, corps décodé).
#: `json` pour GraphQL, `data` pour les échanges de jeton (form-encodé).
Transport = Callable[..., Tuple[int, Any]]


class UpworkAuthError(RuntimeError):
    """Le jeton est absent, expiré sans recours, ou refusé par Upwork."""


class UpworkApiError(RuntimeError):
    """L'API a répondu, mais la réponse n'est pas exploitable."""


@dataclass(frozen=True)
class UpworkCredentials:
    """Identifiants de l'application développeur, tels que délivrés par Upwork."""

    client_id: str
    client_secret: str
    redirect_uri: str


class FileTokenStore:
    """
    Jeton OAuth2 sur disque, hors du dépôt et illisible par les autres comptes.

    Un jeton corrompu est traité comme absent : mieux vaut redemander un
    consentement que planter le scan du matin sur un fichier tronqué.
    """

    def __init__(self, path: Path = DEFAULT_TOKEN_PATH):
        self._path = Path(path)

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("upwork: jeton illisible dans %s (%s)", self._path, exc)
            return None
        return payload if isinstance(payload, dict) else None

    def save(self, token: Dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(token, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self._path)


class UpworkGraphQLClient:
    """Exécute des requêtes GraphQL Upwork en gardant le jeton valide."""

    def __init__(
        self,
        credentials: UpworkCredentials,
        token_store: Any = None,
        transport: Optional[Transport] = None,
        endpoint: str = GRAPHQL_URL,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.credentials = credentials
        self._store = token_store if token_store is not None else FileTokenStore()
        self._transport = transport or _requests_transport
        self._endpoint = endpoint
        self._delay = delay_seconds
        self._clock = clock
        self._sleep = sleeper
        self._last_call: Optional[float] = None
        self._token: Optional[Dict[str, Any]] = None

    # -- Construction ----------------------------------------------------

    @classmethod
    def from_env(cls, **kwargs: Any) -> "UpworkGraphQLClient":
        """
        Assemble le client depuis l'environnement.

        Raises:
            UpworkAuthError: si un identifiant manque — avec le nom de la
            variable à renseigner, pour que le message soit actionnable.
        """
        missing = [
            name
            for name in ("UPWORK_CLIENT_ID", "UPWORK_CLIENT_SECRET")
            if not os.environ.get(name)
        ]
        if missing:
            raise UpworkAuthError(
                "identifiants Upwork absents : "
                + ", ".join(missing)
                + ". Renseigne-les dans .env après approbation de la clé API."
            )

        credentials = UpworkCredentials(
            client_id=os.environ["UPWORK_CLIENT_ID"],
            client_secret=os.environ["UPWORK_CLIENT_SECRET"],
            redirect_uri=os.environ.get(
                "UPWORK_REDIRECT_URI", "http://localhost:8000/oauth/upwork/callback"
            ),
        )
        token_path = os.environ.get("UPWORK_TOKEN_PATH")
        store = FileTokenStore(Path(token_path)) if token_path else FileTokenStore()
        return cls(credentials=credentials, token_store=store, **kwargs)

    # -- Autorisation initiale, faite une seule fois à la main -----------

    def authorization_url(self, state: Optional[str] = None) -> str:
        """Lien de consentement à ouvrir dans un navigateur."""
        params = {
            "response_type": "code",
            "client_id": self.credentials.client_id,
            "redirect_uri": self.credentials.redirect_uri,
        }
        if state:
            params["state"] = state
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> Dict[str, Any]:
        """Échange le code de callback contre un jeton, et le persiste."""
        return self._request_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.credentials.redirect_uri,
            }
        )

    # -- API publique ----------------------------------------------------

    def execute(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Exécute une requête et rend le bloc `data`.

        Un 401 déclenche un rafraîchissement et **une seule** nouvelle
        tentative : au-delà, le jeton est mort et il faut un consentement.

        Raises:
            UpworkAuthError: jeton absent, non renouvelable, ou refusé deux fois.
            UpworkApiError: erreur GraphQL, statut HTTP inattendu, corps illisible.
        """
        payload: Dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables

        status, body = self._post_authenticated(payload)
        if status == 401:
            logger.info("upwork: jeton refusé, rafraîchissement et nouvelle tentative")
            self._refresh()
            status, body = self._post_authenticated(payload)
            if status == 401:
                raise UpworkAuthError(
                    "Upwork refuse le jeton après rafraîchissement : "
                    "relance l'autorisation (authorization_url puis exchange_code)."
                )

        if status != 200:
            raise UpworkApiError(f"Upwork a répondu {status} : {_summarize(body)}")
        if not isinstance(body, dict):
            raise UpworkApiError(f"réponse Upwork illisible : {_summarize(body)}")

        # Une réponse partielle (data + errors) est traitée comme un échec :
        # persister des offres tronquées coûterait plus cher que sauter un scan.
        errors = body.get("errors")
        if errors:
            raise UpworkApiError("erreur GraphQL Upwork : " + _format_errors(errors))

        data = body.get("data")
        if data is None:
            raise UpworkApiError(f"réponse Upwork sans données : {_summarize(body)}")
        return data

    # -- Transport -------------------------------------------------------

    def _post_authenticated(self, payload: Dict[str, Any]) -> Tuple[int, Any]:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }
        self._wait_turn()
        try:
            status, body = self._transport(self._endpoint, json=payload, headers=headers)
        except UpworkAuthError:
            raise
        except Exception as exc:  # réseau, timeout, DNS
            raise UpworkApiError(f"appel Upwork impossible : {exc}") from exc
        self._last_call = self._clock()
        return status, body

    def _wait_turn(self) -> None:
        if self._last_call is None:
            return
        elapsed = self._clock() - self._last_call
        if elapsed < self._delay:
            self._sleep(self._delay - elapsed)

    # -- Cycle de vie du jeton -------------------------------------------

    def _access_token(self) -> str:
        token = self._load_token()
        if self._is_expired(token):
            token = self._refresh()
        access = token.get("access_token")
        if not access:
            raise UpworkAuthError("jeton Upwork sans access_token : refais l'autorisation.")
        return str(access)

    def _load_token(self) -> Dict[str, Any]:
        if self._token is None:
            self._token = self._store.load()
        if not self._token:
            raise UpworkAuthError(
                "aucun jeton Upwork enregistré : l'autorisation initiale n'a pas été faite. "
                "Ouvre authorization_url(), puis passe le code à exchange_code()."
            )
        return self._token

    def _is_expired(self, token: Dict[str, Any]) -> bool:
        expires_at = token.get("expires_at")
        if not isinstance(expires_at, (int, float)):
            return False  # échéance inconnue : on tente, un 401 fera foi
        return self._clock() >= expires_at - EXPIRY_SKEW_SECONDS

    def _refresh(self) -> Dict[str, Any]:
        current = self._load_token()
        refresh_token = current.get("refresh_token")
        if not refresh_token:
            raise UpworkAuthError(
                "jeton Upwork expiré et non renouvelable (pas de refresh_token) : "
                "refais l'autorisation."
            )
        return self._request_token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            previous=current,
        )

    def _request_token(
        self, form: Dict[str, str], previous: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Appelle l'endpoint de jeton et persiste le résultat. Rien n'est écrit en cas d'échec."""
        body_form = dict(form)
        body_form.update(
            {
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
            }
        )
        try:
            status, body = self._transport(
                TOKEN_URL,
                data=body_form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except Exception as exc:
            raise UpworkAuthError(f"échange de jeton Upwork impossible : {exc}") from exc

        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            raise UpworkAuthError(f"Upwork a refusé l'échange de jeton ({status}) : {_summarize(body)}")

        token = dict(body)
        # Upwork ne renvoie pas systématiquement un nouveau refresh token :
        # le perdre obligerait à un consentement manuel au réveil suivant.
        if not token.get("refresh_token") and previous and previous.get("refresh_token"):
            token["refresh_token"] = previous["refresh_token"]

        expires_in = token.get("expires_in")
        if isinstance(expires_in, (int, float, str)):
            try:
                token["expires_at"] = self._clock() + float(expires_in)
            except (TypeError, ValueError):
                pass

        self._token = token
        self._store.save(token)
        return token


# -- Utilitaires ---------------------------------------------------------


def _requests_transport(
    url: str,
    *,
    json: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, str]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Any]:
    """Transport réel. Importé paresseusement : les tests n'ont pas besoin de requests."""
    import requests

    response = requests.post(
        url, json=json, data=data, headers=headers, timeout=DEFAULT_TIMEOUT
    )
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text


def _format_errors(errors: Any) -> str:
    if not isinstance(errors, list):
        return _summarize(errors)
    messages = [
        str(error.get("message", error)) if isinstance(error, dict) else str(error)
        for error in errors
    ]
    return " | ".join(messages[:5])


def _summarize(body: Any) -> str:
    """Rend un corps de réponse lisible dans un log, sans le déverser en entier."""
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    return text[:300]
