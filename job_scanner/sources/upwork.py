"""
upwork.py — Source Upwork, via l'API GraphQL officielle.

Deux réalités distinguent Upwork de Free-Work, et elles gouvernent ce fichier.

**Les Connects sont payants.** Chaque proposition en consomme une dizaine. Le
filtre est donc nettement plus serré qu'ailleurs : plancher de budget, client au
paiement vérifié, annonces déjà noyées sous les propositions écartées. Une offre
ramenée ici doit valoir la dépense — le tri fin reste au scoring, mais le bruit
n'a rien à faire en base.

**Le marché est en dollars.** `RawOffer.currency` porte la devise annoncée par
Upwork ; on ne convertit jamais. Le seul calcul fait ici est un changement
d'unité, du taux horaire vers un équivalent journalier, pour que le champ
`daily_rate_*` garde le même sens d'une plateforme à l'autre. Un forfait, lui,
n'est pas un taux journalier : il reste dans `raw` et ne remplit pas ces champs.

⚠️ L'API ne permet **pas** de soumettre une proposition. Le schéma GraphQL
n'expose aucune mutation de candidature côté freelance (les mutations
`*ClientProposal` servent au client qui reçoit des offres, pas à celui qui
postule), et l'ancienne route REST lève désormais une erreur de dépréciation.
L'envoi reste donc manuel, sur upwork.com. Toute automatisation de navigateur
sur une session Upwork serait un motif de suspension de compte : on ne le fait
pas.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Dict, List, Optional

from job_scanner.models import RawOffer
from job_scanner.sources.base import Fetcher, OfferSource, SearchCriteria
from job_scanner.sources.upwork_client import (
    UpworkApiError,
    UpworkAuthError,
    UpworkGraphQLClient,
)

logger = logging.getLogger(__name__)

JOB_URL_PREFIX = "https://www.upwork.com/jobs/"

# Conversion d'unité, pas de devise : un jour facturé vaut 7 heures travaillées.
# C'est ce qui rend `daily_rate_*` comparable entre un taux horaire Upwork et un
# TJM Free-Work. La devise, elle, reste celle de l'annonce.
HOURS_PER_DAY = 7

_WEEKS_PER_MONTH = 4.345

# Requête par défaut : anglaise, et bornée au périmètre défini dans le profil.
# La syntaxe Lucene partielle d'Upwork accepte les parenthèses et les OR.
DEFAULT_QUERY = (
    '("AI agent" OR "AI agents" OR agentic OR RAG OR "retrieval augmented generation" '
    'OR LLM OR "Claude" OR MCP OR "Model Context Protocol" OR LangChain OR LangGraph)'
)

# Ce que l'API doit rendre pour chaque annonce. Volontairement restreint à ce
# que le modèle pivot et le filtre Connects consomment : élargir la sélection
# coûte du quota et du bruit.
SEARCH_QUERY = """
query JobSearch(
  $marketPlaceJobFilter: MarketplaceJobPostingsSearchFilter
  $searchType: MarketplaceJobPostingSearchType
  $sortAttributes: [MarketplaceJobPostingSearchSortAttribute]
) {
  marketplaceJobPostingsSearch(
    marketPlaceJobFilter: $marketPlaceJobFilter
    searchType: $searchType
    sortAttributes: $sortAttributes
  ) {
    totalCount
    edges {
      cursor
      node {
        id
        title
        description
        ciphertext
        applied
        totalApplicants
        createdDateTime
        publishedDateTime
        experienceLevel
        category
        subcategory
        enterprise
        premium
        amount { rawValue currency displayValue }
        hourlyBudgetType
        hourlyBudgetMin { rawValue currency displayValue }
        hourlyBudgetMax { rawValue currency displayValue }
        weeklyBudget { rawValue currency displayValue }
        engagementDuration { weeks label }
        durationLabel
        skills { name prettyName }
        client {
          totalHires
          totalPostedJobs
          totalReviews
          totalFeedback
          verificationStatus
          location { country }
        }
      }
    }
    pageInfo { endCursor hasNextPage }
  }
}
""".strip()


@dataclass(frozen=True)
class UpworkSearchConfig:
    """
    Ce qui rend le filtre Upwork plus sélectif qu'ailleurs.

    Tous les seuils sont configurables : le marché bouge, et l'utilisateur
    doit pouvoir relever le plancher sans toucher au code.
    """

    query: str = DEFAULT_QUERY
    #: Taux horaire plancher, dans la devise de l'annonce (usuellement USD).
    min_hourly: int = 60
    #: Budget forfaitaire plancher — en dessous, la mission ne vaut pas la dépense.
    min_fixed: int = 2000
    #: Au-delà, l'annonce est déjà saturée et les Connects seraient perdus.
    max_proposals: int = 20
    #: Un client sans paiement vérifié est le premier motif de Connects gâchés.
    verified_payment_only: bool = True
    page_size: int = 50

    @classmethod
    def from_env(cls) -> "UpworkSearchConfig":
        """Lit les seuils depuis l'environnement ; une valeur illisible garde le défaut."""
        defaults = cls()
        return replace(
            defaults,
            query=os.environ.get("UPWORK_QUERY") or defaults.query,
            min_hourly=_env_int("UPWORK_MIN_HOURLY", defaults.min_hourly),
            min_fixed=_env_int("UPWORK_MIN_FIXED", defaults.min_fixed),
            max_proposals=_env_int("UPWORK_MAX_PROPOSALS", defaults.max_proposals),
        )


class UpworkSource(OfferSource):
    """Lit les missions publiées sur la marketplace Upwork."""

    key = "upwork"

    def __init__(
        self,
        client: Any = None,
        config: Optional[UpworkSearchConfig] = None,
        fetcher: Optional[Fetcher] = None,
    ):
        """
        Args:
            client: client GraphQL (injecté dans les tests). À défaut, il est
                construit depuis l'environnement au **premier scan** et non ici :
                tant que la clé API n'est pas approuvée, la seule présence
                d'Upwork dans le registre ne doit pas empêcher les autres
                plateformes de tourner.
            config: seuils de sélectivité.
            fetcher: ignoré. Présent pour que la fabrique du registre garde une
                signature homogène — Upwork parle GraphQL en POST authentifié,
                là où le fetcher commun ne sait faire qu'un GET.
        """
        self._client = client
        self._config = config or UpworkSearchConfig()

    @property
    def client(self) -> Any:
        """Client GraphQL, construit à la demande depuis l'environnement."""
        if self._client is None:
            self._client = UpworkGraphQLClient.from_env()
        return self._client

    # -- API publique ----------------------------------------------------

    def fetch(self, criteria: SearchCriteria) -> List[RawOffer]:
        offers: List[RawOffer] = []
        seen: set = set()
        cursor: Optional[str] = None

        for _ in range(max(1, criteria.max_pages)):
            nodes, cursor = self._read_page(criteria, cursor)
            for node in nodes:
                offer = self._to_offer(node)
                if offer is None or offer.external_id in seen:
                    continue
                if self._is_connects_waste(node, offer):
                    continue
                seen.add(offer.external_id)
                offers.append(offer)

            if not cursor:
                break

        return offers

    # -- Récupération ----------------------------------------------------

    def _read_page(self, criteria: SearchCriteria, cursor: Optional[str]) -> tuple:
        """
        Rend (nœuds, curseur suivant). Une page en échec n'interrompt pas le scan.

        Un jeton manquant fait exception : c'est une action à mener, pas un
        incident réseau, et l'avaler ferait croire à un marché vide.
        """
        variables = {
            "marketPlaceJobFilter": build_search_filter(self._config, criteria, cursor),
            "searchType": "USER_JOBS_SEARCH",
            "sortAttributes": [{"field": "RECENCY"}],
        }
        try:
            data = self.client.execute(SEARCH_QUERY, variables)
        except UpworkAuthError:
            raise
        except (UpworkApiError, Exception) as exc:  # erreur GraphQL, réseau, timeout
            logger.warning("upwork: page inaccessible (%s)", exc)
            return [], None


        payload = (data or {}).get("marketplaceJobPostingsSearch")
        if not isinstance(payload, dict):
            logger.info("upwork: réponse sans résultats exploitables")
            return [], None

        nodes = [
            edge["node"]
            for edge in payload.get("edges") or []
            if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
        ]
        page_info = payload.get("pageInfo") or {}
        next_cursor = page_info.get("endCursor") if page_info.get("hasNextPage") else None
        return nodes, next_cursor

    # -- Traduction vers le modèle pivot ---------------------------------

    def _to_offer(self, node: Dict[str, Any]) -> Optional[RawOffer]:
        """Traduit une annonce Upwork ; rend None si elle est inexploitable."""
        external_id, title = node.get("id"), node.get("title")
        url = _job_url(node.get("ciphertext"))
        if not external_id or not title or not url:
            return None

        low, high, currency, contract_type = _budget(node)

        try:
            return RawOffer(
                source=self.key,
                external_id=external_id,
                url=url,
                title=title,
                company=None,  # la marketplace ne publie pas le nom du client
                description=(node.get("description") or "").strip() or None,
                published_at=_parse_date(
                    node.get("publishedDateTime") or node.get("createdDateTime")
                ),
                daily_rate_min=low,
                daily_rate_max=high,
                currency=currency,
                duration_months=_duration_in_months(node),
                # Upwork est distant par construction. Le taire ferait perdre à
                # chaque mission les points de condition qu'elle mérite.
                remote_mode="full",
                location=_client_country(node),
                contract_type=contract_type,
                skills=_skill_names(node.get("skills")),
                raw=_raw_payload(node),
            )
        except Exception as exc:  # schéma inattendu : on saute cette annonce
            logger.warning("upwork: annonce %s ignorée (%s)", external_id, exc)
            return None

    # -- Discipline Connects ---------------------------------------------

    def _is_connects_waste(self, node: Dict[str, Any], offer: RawOffer) -> bool:
        """
        Écarte ce sur quoi il serait absurde de dépenser des Connects.

        Un budget absent n'est **pas** un motif d'exclusion : masquer le budget
        est courant chez les clients sérieux. Le seuil de score s'en chargera.
        """
        config = self._config

        if node.get("applied") is True:
            logger.debug("upwork: %s déjà candidatée", offer.external_id)
            return True

        applicants = node.get("totalApplicants")
        if isinstance(applicants, (int, float)) and applicants > config.max_proposals:
            return True

        if config.verified_payment_only and not _payment_verified(node):
            return True

        hourly_ceiling = _amount(node.get("hourlyBudgetMax")) or _amount(node.get("hourlyBudgetMin"))
        if hourly_ceiling is not None and hourly_ceiling < config.min_hourly:
            return True

        if offer.contract_type == "fixed":
            budget = _amount(node.get("amount"))
            if budget is not None and 0 < budget < config.min_fixed:
                return True

        return False


# -- Construction de la requête ------------------------------------------


def build_search_filter(
    config: UpworkSearchConfig, criteria: SearchCriteria, cursor: Optional[str]
) -> Dict[str, Any]:
    """
    Traduit les critères en filtre Upwork.

    `criteria.query` est délibérément ignoré : il porte la requête française du
    scan quotidien (« intelligence artificielle »), qui ne ramène rien sur un
    marché anglophone. La requête Upwork vit dans sa propre configuration.
    """
    pagination: Dict[str, Any] = {"first": config.page_size}
    if cursor:
        pagination["after"] = cursor

    return {
        "searchExpression_eq": config.query,
        "hourlyRate_eq": {"rangeStart": config.min_hourly},
        "budgetRange_eq": {"rangeStart": config.min_fixed},
        "proposalRange_eq": {"rangeEnd": config.max_proposals},
        "verifiedPaymentOnly_eq": config.verified_payment_only,
        "pagination_eq": pagination,
    }


# -- Utilitaires de parsing ----------------------------------------------


def _job_url(ciphertext: Any) -> Optional[str]:
    """
    Construit le lien vers l'annonce.

    Upwork range ses offres sous `/jobs/~<ciphertext>`. Le tilde fait partie de
    l'identifiant mais n'est pas toujours présent dans le champ : sans lui,
    l'URL renvoie vers la recherche au lieu de l'annonce.

    ⚠️ À confirmer au premier scan réel. Le node de recherche ne porte aucun
    champ `url` : `ciphertext` est la seule voie, et c'est ce chemin qu'emploie
    l'interface Upwork connectée. Il n'a pas pu être validé de bout en bout ici,
    Cloudflare bloquant toute vérification automatisée (403 en HTTP, challenge
    Turnstile en navigateur). Point rassurant : contrairement au piège Free-Work,
    aucune redirection n'est émise vers l'accueil. Un second format existe pour
    le référencement, `/freelance-jobs/apply/<slug>_~<ciphertext>/` ; si le lien
    ci-dessous s'avérait mauvais, c'est celui-là qu'il faudrait construire.
    """
    if not isinstance(ciphertext, str) or not ciphertext.strip():
        return None
    token = ciphertext.strip()
    return f"{JOB_URL_PREFIX}{token if token.startswith('~') else '~' + token}"


def _budget(node: Dict[str, Any]) -> tuple:
    """
    Rend (min, max, devise, type de contrat).

    Horaire : le taux est ramené à un équivalent journalier pour rester
    comparable au TJM des autres plateformes. Forfait : aucun taux journalier
    n'est inventé — le montant reste dans `raw`.
    """
    low = _amount(node.get("hourlyBudgetMin"))
    high = _amount(node.get("hourlyBudgetMax"))

    if low is not None or high is not None:
        currency = _currency(node.get("hourlyBudgetMin")) or _currency(node.get("hourlyBudgetMax"))
        bounds = [value for value in (low, high) if value is not None]
        return (
            round(min(bounds) * HOURS_PER_DAY),
            round(max(bounds) * HOURS_PER_DAY),
            currency or "USD",
            "hourly",
        )

    fixed = _amount(node.get("amount"))
    if fixed:
        return None, None, _currency(node.get("amount")) or "USD", "fixed"

    # Budget masqué : le type se déduit de la présence d'un cadre horaire.
    contract_type = "hourly" if node.get("hourlyBudgetType") else "fixed"
    return None, None, _currency(node.get("amount")) or "USD", contract_type


def _amount(money: Any) -> Optional[float]:
    """Extrait la valeur numérique d'un `Money` Upwork (`rawValue` est une chaîne)."""
    if not isinstance(money, dict):
        return None
    try:
        return float(money.get("rawValue"))
    except (TypeError, ValueError):
        return None


def _currency(money: Any) -> Optional[str]:
    if not isinstance(money, dict):
        return None
    currency = money.get("currency")
    return currency.strip().upper() if isinstance(currency, str) and currency.strip() else None


def _payment_verified(node: Dict[str, Any]) -> bool:
    client = node.get("client")
    if not isinstance(client, dict):
        return False
    status = client.get("verificationStatus")
    return isinstance(status, str) and status.strip().upper() == "VERIFIED"


def _client_country(node: Dict[str, Any]) -> Optional[str]:
    client = node.get("client")
    location = client.get("location") if isinstance(client, dict) else None
    country = location.get("country") if isinstance(location, dict) else None
    return country if isinstance(country, str) and country.strip() else None


def _duration_in_months(node: Dict[str, Any]) -> Optional[int]:
    duration = node.get("engagementDuration")
    weeks = duration.get("weeks") if isinstance(duration, dict) else None
    if not isinstance(weeks, (int, float)) or weeks <= 0:
        return None
    return max(1, round(weeks / _WEEKS_PER_MONTH))


def _skill_names(skills: Any) -> List[str]:
    """Préfère le libellé lisible (`prettyName`) au nom canonique en minuscules."""
    if not isinstance(skills, list):
        return []
    names: List[str] = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        label = skill.get("prettyName") or skill.get("name")
        if isinstance(label, str) and label.strip():
            names.append(label.strip())
    return names


def _raw_payload(node: Dict[str, Any]) -> Dict[str, Any]:
    """
    Conserve l'annonce, en remontant le budget forfaitaire au premier niveau.

    Le forfait ne remplit pas `daily_rate_*` ; sans ce raccourci, il faudrait
    fouiller `raw` pour savoir ce que vaut la mission.
    """
    payload = dict(node)
    fixed = _amount(node.get("amount"))
    if fixed:
        payload["budget_amount"] = fixed
    return payload


def _parse_date(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("upwork: %s illisible (%r), valeur par défaut %s", name, raw, default)
        return default
