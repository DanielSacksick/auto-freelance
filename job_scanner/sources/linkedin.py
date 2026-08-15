"""linkedin.py — Source LinkedIn Jobs, via Jina Reader.

LinkedIn Jobs est la plateforme la plus verrouillée du lot :

- Les pages sont servies en JavaScript (React) : pas de HTML statique.
- Le `robots.txt` interdit `/jobs/*` : pas de scrape autorisé.
- Les requêtes HTTP pures reçoivent une page de login ou un CAPTCHA.
- Jina Reader (r.jina.ai) bloque le domaine pour cause d'abus.

Deux chemins existent, aucun n'est idéal :

1. **Playwright headless** — ouvre linkedin.com, utilise une session
   authentifiée existante (cookies du profil), lit le DOM rendu.
   Risque : LinkedIn détecte les navigateurs headless et peut restreindre
   le compte.

2. **Google Jobs** — Google agrège les offres LinkedIn + Indeed + Welcome to
   the Jungle dans son widget Jobs, accessible via Jina Reader.
   Compromis acceptable : moins d'offres, mais zéro risque compte.

Ce module implémente le chemin 2 (Google Jobs) comme source principale,
et laisse un placeholder pour Playwright quand il sera configuré.

Pour activer LinkedIn plus tard :
    1. Configurer Playwright : pip install playwright && playwright install chromium
    2. Exporter les cookies LinkedIn dans un fichier (linkedin_cookies.json)
    3. Décommenter le code Playwright ci-dessous
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urlencode

from job_scanner.models import RawOffer
from job_scanner.sources.base import Fetcher, OfferSource, SearchCriteria

logger = logging.getLogger(__name__)

# Google Jobs URL — agrège LinkedIn + Indeed + Welcome to the Jungle
GOOGLE_JOBS_URL = "https://www.google.com/search"

# On utilise le paramètre `udm=8` qui active le mode Jobs de Google
DEFAULT_QUERY = "(freelance OR contract) (\"AI agent\" OR LLM OR Claude OR RAG OR MCP)"

# Regex pour extraire les offres d'une page Google Jobs
# Le format Google Jobs en mode texte (via Jina Reader) varie beaucoup.
# On cherche des patterns basiques : titres, entreprises, lieux.
_JOB_TITLE_RE = re.compile(r"^(\d+\.?)\s*(.+?)(?:\s*–\s*|\s*-\s*)(.+?)(?:\s*•\s*|\s*['']\s*)(.+)$", re.MULTILINE)

# Fallback : détection de blocs d'offres dans le markdown
_OFFER_BLOCK_RE = re.compile(
    r"(?:^|\n)(\d+)\.\s+\[([^\]]+)\]\(([^)]+)\)\s*(?:\n|$)",
    re.MULTILINE,
)

# Extraction d'entreprise et lieu depuis les lignes
_COMPANY_LOCATION_RE = re.compile(
    r"(?:·|•)\s*([^·•]+?)\s*(?:·|•)\s*([^·•]+)",
)


class LinkedInJobsSource(OfferSource):
    """Lit les offres freelance publiées sur LinkedIn Jobs (via Google Jobs).

    Contournement : Google Jobs agrège les offres LinkedIn, Indeed, Welcome to
    the Jungle, etc. dans un widget accessible sans authentification. Jina Reader
    (r.jina.ai) peut le lire tant que le trafic reste modéré.

    Limites connues :
    - Google limite à ~20 résultats par requête
    - Certaines offres sont dupliquées entre agrégateurs
    - Les TJM sont rarement publiés
    """

    key = "linkedin"

    def __init__(
        self,
        fetcher: Optional[Fetcher] = None,
        base_url: str = GOOGLE_JOBS_URL,
        country: str = "France",
    ):
        """
        Args:
            fetcher: injecté pour la compatibilité ; ignoré car on passe par
                Jina Reader directement.
            base_url: URL de recherche Google Jobs.
            country: pays cible pour le filtre de localisation.
        """
        self._fetcher = fetcher
        self._base_url = base_url.rstrip("/")
        self._country = country

    # -- API publique ----------------------------------------------------

    def fetch(self, criteria: SearchCriteria) -> List[RawOffer]:
        """Interroge Google Jobs via Jina Reader ; rend les offres trouvées."""
        offers: List[RawOffer] = []
        seen: set = set()

        query = criteria.query or DEFAULT_QUERY
        location = criteria.extra.get("location", self._country)

        for page in range(1, max(1, criteria.max_pages) + 1):
            url = self._build_url(query, location, page)
            html = self._fetch_via_jina(url)
            if not html:
                logger.info("linkedin: aucune donnée récupérée depuis Google Jobs")
                break

            parsed = self._parse_page(html, url)
            for offer in parsed:
                if offer.external_id in seen:
                    continue
                seen.add(offer.external_id)
                offers.append(offer)

            # Une page sans résultats est la dernière
            if len(parsed) < 10:
                break

        logger.info("linkedin: %d offres trouvées via Google Jobs", len(offers))
        return offers

    # -- Récupération ----------------------------------------------------

    def _build_url(self, query: str, location: str, page: int) -> str:
        """Construit l'URL de recherche Google Jobs."""
        full_query = f"{query} freelance {location}"
        params: Dict[str, Any] = {
            "q": full_query,
            "udm": "8",           # mode Jobs
            "ibp": "htl;jobs",    # mode emploi
        }
        if page > 1:
            params["start"] = (page - 1) * 10
        return f"{self._base_url}?{urlencode(params, safe=':,')}"

    def _fetch_via_jina(self, url: str) -> Optional[str]:
        """Récupère une page via Jina Reader (r.jina.ai)."""
        import urllib.request

        jina_url = f"https://r.jina.ai/{url}"
        req = urllib.request.Request(
            jina_url,
            headers={
                "Accept": "text/plain",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("linkedin: Jina Reader inaccessible pour %s (%s)", url[:60], exc)
            return None

        # Vérifier que Jina n'a pas bloqué
        if not content or "blocked" in content[:200].lower() or "forbidden" in content[:200].lower():
            logger.warning("linkedin: Jina Reader a bloqué l'accès à Google Jobs")
            return None

        return content

    # -- Parsing ---------------------------------------------------------

    def _parse_page(self, html: str, source_url: str) -> List[RawOffer]:
        """Extrait les offres du markdown rendu par Jina Reader.

        Le format Google Jobs via Jina Reader varie énormément selon la
        localisation et le jour. On tente plusieurs stratégies de parsing.
        """
        offers: List[RawOffer] = []
        lines = html.split("\n")
        titles_seen: set = set()

        # Stratégie 1 : chercher des patterns titre + description
        i = 0
        while i < len(lines) and len(offers) < 30:
            line = lines[i].strip()

            # Chercher les lignes qui ressemblent à des titres d'offres
            # (format Google Jobs markdown : liens numérotés ou titres en gras)
            title_match = re.match(r"^(\d+)\.\s+\*\*(.+?)\*\*", line)

            # Chercher aussi les lignes avec [titre](url)
            if not title_match:
                # Format: [Titre du poste](url)
                link_match = re.match(r"^\d+\.\s*\[([^\]]+)\]\(([^)]+)\)", line)
                if link_match:
                    title = link_match.group(1)
                    url = link_match.group(2)
                else:
                    i += 1
                    continue
            else:
                title = title_match.group(2)
                url = source_url

            # Nettoyer le titre
            title = re.sub(r"\s+", " ", title).strip()
            if not title or len(title) < 5 or title.lower().startswith("about this"):
                i += 1
                continue

            if title in titles_seen:
                i += 1
                continue
            titles_seen.add(title)

            # Lire les lignes suivantes pour trouver entreprise/lieu/description
            company = None
            location = None
            description_lines: List[str] = []
            j = i + 1

            while j < len(lines) and j < i + 12:
                next_line = lines[j].strip()
                if not next_line:
                    j += 1
                    continue

                # Détection entreprise (souvent juste après le titre)
                if company is None and re.match(r"^[A-Z][a-zA-Z0-9\s\-\.]{2,40}$", next_line) and len(next_line) < 80:
                    # Vérifier que ça ressemble à une entreprise (pas un gabarit)
                    if not any(kw in next_line.lower() for kw in ["about", "page", "source", "url", "http"]):
                        if not next_line.startswith("(") and not next_line.endswith(")"):
                            company = next_line
                            j += 1
                            continue

                # Détection lieu (souvent après l'entreprise)
                if location is None and company and re.match(r"^[A-Z][a-zA-Z\s\-]{2,}$", next_line) and len(next_line) < 60:
                    location = next_line
                    j += 1
                    continue

                # Accumuler la description
                if len(next_line) > 40 and not next_line.startswith("http"):
                    description_lines.append(next_line)
                    if len(description_lines) >= 3:
                        break

                j += 1

            description = " ".join(description_lines) if description_lines else None
            external_id = self._make_id(title, company or "linkedin")

            try:
                offer = RawOffer(
                    source=self.key,
                    external_id=external_id,
                    url=url,
                    title=title[:300],
                    company=company[:200] if company else None,
                    description=description[:2000] if description else None,
                    published_at=datetime.now(),  # Google Jobs ne donne pas la date
                    daily_rate_min=None,  # rarement publié
                    daily_rate_max=None,
                    currency="EUR",
                    location=location[:100] if location else "France",
                    contract_type="contractor",
                    raw={"source_url": source_url, "matched_via": "google_jobs"},
                )
                offers.append(offer)
            except Exception as exc:
                logger.debug("linkedin: offre ignorée (%s)", exc)

            i = j

        # Stratégie 2 : si rien trouvé avec la stratégie 1, chercher des patterns
        # plus larges dans tout le texte
        if not offers:
            offers = self._fallback_parse(html, source_url)

        return offers

    def _fallback_parse(self, html: str, source_url: str) -> List[RawOffer]:
        """Parsing de fallback — recherche les URLs d'offres dans le texte."""
        offers: List[RawOffer] = []
        seen_urls: set = set()

        # Chercher les URLs LinkedIn ou Indeed
        for match in re.finditer(
            r'(https?://(?:www\.)?linkedin\.com/jobs/view/[^\s)"\']+|https?://(?:www\.)?indeed\.fr[^\s)"\']+)',
            html,
        ):
            url = match.group(1).rstrip(".,;")
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Essayer d'extraire un titre du contexte
            start = max(0, match.start() - 200)
            context = html[start:match.start()]
            title_match = re.search(r'\*\*(.+?)\*\*', context)

            external_id = self._make_id(url, "linkedin")
            try:
                offer = RawOffer(
                    source=self.key,
                    external_id=external_id,
                    url=url,
                    title=title_match.group(1)[:300] if title_match else "Offre LinkedIn",
                    description=None,
                    published_at=datetime.now(),
                    daily_rate_min=None,
                    daily_rate_max=None,
                    currency="EUR",
                    contract_type="contractor",
                    raw={"source_url": source_url, "matched_via": "fallback"},
                )
                offers.append(offer)
            except Exception:
                continue

        return offers

    # -- Utilitaires -----------------------------------------------------

    @staticmethod
    def _make_id(title: str, context: str) -> str:
        """Génère un identifiant stable à partir du titre."""
        import hashlib
        raw = f"{title.strip().lower()}:{context.strip().lower()}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]


# Placeholder pour la version Playwright — à décommenter quand Playwright
# sera configuré avec une session LinkedIn authentifiée.
#
# class LinkedInPlaywrightSource(OfferSource):
#     """Version Playwright : ouvre LinkedIn Jobs dans un navigateur headless
#     avec une session authentifiée. Plus fiable mais plus risqué."""
#
#     key = "linkedin"
#
#     def fetch(self, criteria: SearchCriteria) -> List[RawOffer]:
#         raise NotImplementedError(
#             "LinkedIn Playwright n'est pas encore activé. "
#             "Configure les cookies LinkedIn dans linkedin_cookies.json "
#             "et installe Playwright : pip install playwright && playwright install chromium"
#         )