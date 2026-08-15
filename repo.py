"""
repo.py — Persistance SQLite du pipeline auto-freelance.

Remplace `pg_repo.py` (Postgres, spécifique à l'infrastructure interne) par
une base SQLite locale : stdlib pur (`sqlite3`), un seul fichier, prête à
l'emploi dès le premier `git clone`. Le contrat reste le même que l'original :
dédup par `(source, external_id)`, statuts `candidate` → `applied`, offre
complète (+ scoring, + rédaction) conservée dans une colonne `notes` (JSON).

Chemin de la base : `AUTOFREELANCE_DB` (variable d'environnement), sinon
`~/.auto-freelance/pipeline.db`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DB_ENV = "AUTOFREELANCE_DB"
DEFAULT_DB_PATH = "~/.auto-freelance/pipeline.db"

# TTL de déduplication : une offre déjà candidatée (applied) ou écartée
# (skipped) redevient candidate si on la revoit après ce délai — mieux vaut la
# retraiter que la supprimer à jamais sur la foi d'un scan vieux d'un mois.
DEDUP_DAYS = 30

# Champs du modèle pivot (+ scoring/rédaction) conservés dans `notes` (JSON).
NOTES_KEYS = {
    "external_id", "url", "title", "company", "description", "published_at",
    "daily_rate_min", "daily_rate_max", "currency", "duration_months",
    "remote_mode", "location", "contract_type", "skills", "source",
    "fit_score", "fit_reasons", "draft_body", "draft_model",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    url TEXT,
    status TEXT NOT NULL DEFAULT 'candidate',
    fit_score REAL,
    notes TEXT NOT NULL DEFAULT '{}',
    notified_at TEXT,
    applied_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status);
CREATE INDEX IF NOT EXISTS idx_offers_fit_score ON offers(fit_score);
"""


def offer_to_dict(offer: Any, **extra: Any) -> Dict[str, Any]:
    """Convertit un `RawOffer` (Pydantic) en dict, avec champs additionnels.

    Utile pour ajouter des données de scoring (`fit_score`, `fit_reasons`) ou
    de rédaction (`draft_body`, `draft_model`) qui ne font pas partie du
    modèle Pydantic. Un dict est retourné tel quel, fusionné avec `extra`.
    """
    if isinstance(offer, dict):
        return {**offer, **extra}
    data: Dict[str, Any] = {}
    for key in NOTES_KEYS:
        if hasattr(offer, key):
            value = getattr(offer, key)
            if value is not None:
                data[key] = value.isoformat() if isinstance(value, datetime) else value
    data.update(extra)
    data.setdefault("external_id", str(getattr(offer, "external_id", "")))
    return data


def default_db_path() -> Path:
    raw = os.environ.get(DB_ENV) or DEFAULT_DB_PATH
    return Path(raw).expanduser()


class SqliteRepo:
    """Persiste les offres dans une base SQLite locale, déduplique par (source, external_id)."""

    def __init__(self, db_path: Optional[Path] = None):
        self._path = Path(db_path) if db_path else default_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- API publique --------------------------------------------------------

    def record_scan(self, offers: List[Any]) -> Dict[str, int]:
        """Insère ou met à jour les offres scannées ; rend {seen, new, updated}.

        `offers` peut mélanger des `RawOffer` et des dicts (sortie de
        `offer_to_dict`, utilisée pour persister score/draft après coup).
        """
        seen = new = updated = 0
        for offer in offers:
            outcome = self._upsert(offer)
            if outcome == "new":
                new += 1
            elif outcome == "updated":
                updated += 1
            elif outcome == "seen":
                seen += 1
        logger.info(
            "repo: %d nouvelle(s), %d mise(s) à jour, %d déjà vue(s)", new, updated, seen
        )
        return {"seen": seen + new + updated, "new": new, "updated": updated}

    def offers_pending_notification(
        self, min_score: float = 0.0, max_results: int = 10
    ) -> List[Dict[str, Any]]:
        """Offres candidates, rédigées, non encore notifiées, au-dessus du seuil."""
        rows = self._conn.execute(
            "SELECT * FROM offers "
            "WHERE status = 'candidate' AND notified_at IS NULL "
            "  AND fit_score IS NOT NULL AND fit_score >= ? "
            "  AND json_extract(notes, '$.draft_body') IS NOT NULL "
            "  AND json_extract(notes, '$.draft_body') != '' "
            "ORDER BY fit_score DESC LIMIT ?",
            (min_score, max_results),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def offers_ready_to_submit(self, max_results: Optional[int] = None) -> List[Dict[str, Any]]:
        """Offres rédigées et pas encore soumises, dans la forme attendue par
        `submit.py::submit_offers()` (miroir de la sortie de `draft.py`).

        Utilisée par `pipeline.py --submit-only` pour reprendre une soumission
        sans repasser par scan/score/draft.
        """
        query = (
            "SELECT * FROM offers "
            "WHERE status = 'candidate' "
            "  AND json_extract(notes, '$.draft_body') IS NOT NULL "
            "  AND json_extract(notes, '$.draft_body') != '' "
            "ORDER BY fit_score DESC"
        )
        params: tuple = ()
        if max_results is not None:
            query += " LIMIT ?"
            params = (max_results,)
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_drafted(row) for row in rows]

    def mark_notified(self, ids: List[Any]) -> None:
        """Marque des offres comme notifiées (`ids` = clé primaire `offers.id`)."""
        if not ids:
            return
        now = _now_iso()
        self._conn.executemany(
            "UPDATE offers SET notified_at = ?, updated_at = ? WHERE id = ?",
            [(now, now, int(i)) for i in ids],
        )
        self._conn.commit()

    def mark_applied(self, source: str, external_id: str, proof: Dict[str, Any]) -> None:
        """Marque une offre comme candidatée avec succès.

        À n'appeler qu'après une `SubmissionResult.submitted is True` — jamais
        spéculativement (voir job_scanner/submission/playwright.py).
        """
        now = _now_iso()
        row = self._conn.execute(
            "SELECT notes FROM offers WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        if row is None:
            logger.warning(
                "repo: mark_applied appelé pour %s/%s, offre introuvable en base",
                source, external_id,
            )
            return

        notes = json.loads(row["notes"]) if row["notes"] else {}
        notes["applied_proof"] = proof
        notes["applied_at"] = now

        self._conn.execute(
            "UPDATE offers SET status = 'applied', applied_at = ?, updated_at = ?, notes = ? "
            "WHERE source = ? AND external_id = ?",
            (now, now, json.dumps(notes, default=str, ensure_ascii=False), source, external_id),
        )
        self._conn.commit()
        logger.info("repo: %s/%s → applied", source, external_id)

    def close(self) -> None:
        self._conn.close()

    # -- Interne --------------------------------------------------------------

    def _upsert(self, offer: Any) -> str:
        """Insère ou fusionne une offre. Rend 'new', 'updated', 'seen' ou 'skipped'."""
        payload = offer if isinstance(offer, dict) else offer_to_dict(offer)
        source = payload.get("source") or ""
        external_id = str(payload.get("external_id") or "")
        title = payload.get("title") or ""
        company = payload.get("company")
        url = payload.get("url")

        if not source or not external_id or not title:
            logger.warning("repo: offre ignorée (source/external_id/title manquant)")
            return "skipped"

        now = _now_iso()
        notes_payload: Dict[str, Any] = {
            key: value for key, value in payload.items() if key in NOTES_KEYS and value is not None
        }
        for key, value in list(notes_payload.items()):
            if isinstance(value, datetime):
                notes_payload[key] = value.isoformat()
        notes_payload.setdefault("external_id", external_id)

        existing = self._conn.execute(
            "SELECT id, status, notes, updated_at FROM offers WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()

        if existing is None:
            self._conn.execute(
                "INSERT INTO offers "
                "(source, external_id, title, company, url, status, fit_score, notes, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?)",
                (
                    source, external_id, title[:500], company, url,
                    notes_payload.get("fit_score"),
                    json.dumps(notes_payload, default=str, ensure_ascii=False),
                    now, now,
                ),
            )
            self._conn.commit()
            return "new"

        resurfacing = existing["status"] in ("applied", "skipped")
        if resurfacing and not self._is_stale(existing["updated_at"]):
            # Déjà candidatée/écartée récemment : suppression silencieuse.
            return "seen"

        old_notes = json.loads(existing["notes"]) if existing["notes"] else {}
        has_new_info = notes_payload.get("fit_score") is not None or bool(notes_payload.get("draft_body"))

        if not has_new_info and not resurfacing:
            # Rescan sans rien de neuf (pas de score, pas de draft) : inutile d'écrire.
            return "seen"

        # Un score plus bas ne doit jamais écraser un meilleur score déjà noté.
        old_score = old_notes.get("fit_score")
        new_score = notes_payload.get("fit_score")
        if new_score is not None and old_score is not None and float(new_score) < float(old_score):
            notes_payload["fit_score"] = old_score
            notes_payload["fit_reasons"] = old_notes.get("fit_reasons")

        combined = {**old_notes, **notes_payload}
        new_status = "candidate" if resurfacing else existing["status"]

        self._conn.execute(
            "UPDATE offers SET title = ?, company = ?, url = ?, status = ?, fit_score = ?, "
            "notes = ?, updated_at = ? WHERE id = ?",
            (
                title[:500], company, url, new_status, combined.get("fit_score"),
                json.dumps(combined, default=str, ensure_ascii=False), now, existing["id"],
            ),
        )
        self._conn.commit()
        return "new" if resurfacing else "updated"

    @staticmethod
    def _is_stale(updated_at: str) -> bool:
        try:
            updated = datetime.fromisoformat(updated_at)
        except (TypeError, ValueError):
            return True
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - updated) > timedelta(days=DEDUP_DAYS)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        notes = json.loads(row["notes"]) if row["notes"] else {}
        return {
            "id": row["id"],
            "source": row["source"],
            "external_id": row["external_id"],
            "title": notes.get("title", row["title"]),
            "company": notes.get("company", row["company"]),
            "url": notes.get("url", row["url"]),
            "status": row["status"],
            "fit_score": row["fit_score"],
            "fit_reasons": notes.get("fit_reasons"),
            "draft_body": notes.get("draft_body"),
            "draft_model": notes.get("draft_model"),
            "daily_rate_min": notes.get("daily_rate_min"),
            "daily_rate_max": notes.get("daily_rate_max"),
            "currency": notes.get("currency", "EUR"),
            "duration_months": notes.get("duration_months"),
            "remote_mode": notes.get("remote_mode"),
            "location": notes.get("location"),
            "notes": notes,
        }

    @staticmethod
    def _row_to_drafted(row: sqlite3.Row) -> Dict[str, Any]:
        """Reconstruit `{"offer": RawOffer, "score", "reasons", "draft_body",
        "draft_model"}` — la forme que `submit.py::submit_offers()` attend en
        entrée, symétrique de la sortie de `draft.py`."""
        from job_scanner.models import RawOffer

        notes = json.loads(row["notes"]) if row["notes"] else {}
        offer_fields = {key: value for key, value in notes.items() if key in RawOffer.model_fields}
        offer_fields.setdefault("source", row["source"])
        offer_fields.setdefault("external_id", row["external_id"])
        offer_fields.setdefault("url", row["url"] or notes.get("url") or "")
        offer_fields.setdefault("title", row["title"])
        raw_offer = RawOffer(**offer_fields)

        return {
            "offer": raw_offer,
            "score": row["fit_score"],
            "reasons": notes.get("fit_reasons") or {},
            "draft_body": notes.get("draft_body") or "",
            "draft_model": notes.get("draft_model") or "",
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
