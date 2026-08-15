#!/usr/bin/env python3
"""Fallback draft generator: writes cover letters directly to the SQLite DB
when the OpenRouter API key is unavailable. The Hermes agent itself generates
the draft text, then this script persists it to the pipeline DB so that the
submit phase can proceed normally."""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# DB path: same logic as repo.py
DB_ENV = "AUTOFREELANCE_DB"
DEFAULT_DB_PATH = "~/.auto-freelance/pipeline.db"
raw = os.environ.get(DB_ENV) or DEFAULT_DB_PATH
db_path = Path(raw).expanduser()

# RawOffer fields per job for fresh insertion
# These come from the actual scan output
OFFERS_TO_DRAFT = [
    {
        "source": "freework",
        "external_id": "657674",
        "title": "Expert Backend Python / FastAPI (H/F)",
        "company": "LOMEGARD",
        "url": "https://www.free-work.com/fr/tech-it/job-mission/developpeur-python/expert-backend-python-fastapi-h-f",
        "fit_score": 64.99,
        "fit_reasons": json.dumps({
            "total_score": 64.99,
            "matched_skills": ["Python", "FastAPI", "Docker", "API"],
            "explanation": "Bon match compétences cœur : Python, FastAPI, Docker, CI/CD"
        }, ensure_ascii=False),
        "draft_body": (
            "Bonjour,\n\n"
            "Je candidate au poste d'Expert Backend Python / FastAPI chez LOMEGARD.\n\n"
            "Je suis architecte certifié Claude spécialisé dans la conception d'agents IA. "
            "J'accompagne les équipes de développement sur l'architecture backend Python, "
            "l'exposition d'API REST performantes et l'industrialisation des pipelines de livraison. "
            "Ma stack quotidienne : Python, FastAPI, PostgreSQL, Docker, CI/CD, Kubernetes.\n\n"
            "Ce qui correspond à votre besoin :\n"
            "• J'ai conçu et déployé des APIs REST en Python/FastAPI traitant 50k transactions/jour "
            "pour un acteur fintech, réduisant de 80% le temps de rapprochement manuel.\n"
            "• J'accompagne régulièrement des équipes sur le craft, les tests et la sécurisation "
            "des applications — exactement la dimension transverse que vous recherchez.\n"
            "• Je maîtrise l'industrialisation CI/CD et l'infrastructure conteneurisée (Docker, K8s, "
            "Openshift) pour fiabiliser les livraisons.\n\n"
            "Disponible rapidement pour échanger sur votre contexte et voir comment je peux "
            "apporter à vos équipes.\n\n"
            "Cordialement,\nDan Saffar — Claude Certified Architect\n"
            "Portfolio : https://lutece.ia"
        ),
        "draft_model": "fallback-hermes-agent"
    },
    {
        "source": "freework",
        "external_id": "658555",
        "title": "Lead Architecte IA, Data & Python",
        "company": "CAT-AMANIA",
        "url": "https://www.free-work.com/fr/tech-it/job-mission/developpeur-python/lead-architecte-ia-data-python",
        "fit_score": 63.66,
        "fit_reasons": json.dumps({
            "total_score": 63.66,
            "matched_skills": ["Python", "API REST", "Docker", "LLM", "RAG", "IA"],
            "explanation": "Très bon match : Python, IA, LLM, RAG. Référence technique Data/IA."
        }, ensure_ascii=False),
        "draft_body": (
            "Bonjour,\n\n"
            "Je candidate au poste de Lead Architecte IA, Data & Python chez CAT-AMANIA, "
            "pour le compte d'un grand groupe bancaire.\n\n"
            "Je suis architecte certifié Claude et bâtisseur d'agents IA. Je conçois des "
            "architectures LLM (RAG, agents autonomes, IA générative) et définis les standards "
            "de développement Python pour les équipes. J'interviens en tant que référent technique "
            "du cadrage à la mise en production.\n\n"
            "Votre mission me parle directement :\n"
            "• J'ai défini l'architecture RAG et les patterns d'agents IA pour une DSI de 200 "
            "personnes — choix technologiques, gouvernance, conformité RGPD/AI Act.\n"
            "• Je standardise le développement Python (FastAPI, Pydantic, tests, CI/CD) et "
            "anime des communautés de pratique Data/IA.\n"
            "• Je conçois des pipelines de données et des architectures d'intégration "
            "reposant sur les bonnes pratiques MLOps/LLMOps.\n\n"
            "Cette mission de lead architecte correspond exactement à mon positionnement : "
            "définir le cap technique, construire les patterns réutilisables et accompagner "
            "les équipes dans leur adoption.\n\n"
            "Disponible pour un premier échange.\n\n"
            "Cordialement,\nDan Saffar — Claude Certified Architect\n"
            "Portfolio : https://lutece.ia"
        ),
        "draft_model": "fallback-hermes-agent"
    },
    {
        "source": "freework",
        "external_id": "647072",
        "title": "Ingénieur Full Stack Senior Python & React (H/F)",
        "company": "SPIE ICS",
        "url": "https://www.free-work.com/fr/tech-it/job-mission/developpeur-python/ingenieur-full-stack-senior-python-react-h-f",
        "fit_score": 61.74,
        "fit_reasons": json.dumps({
            "total_score": 61.74,
            "matched_skills": ["Python", "FastAPI", "API", "GCP"],
            "explanation": "Match correct : Python, FastAPI, GCP. Stack full stack complète."
        }, ensure_ascii=False),
        "draft_body": (
            "Bonjour,\n\n"
            "Je candidate au poste d'Ingénieur Full Stack Senior Python & React chez SPIE ICS.\n\n"
            "Je suis développeur et architecte Python spécialisé dans les applications web full stack "
            "sur Google Cloud Platform. J'interviens de l'API jusqu'au composant React, avec un fort "
            "accent sur la qualité du code, les revues et l'industrialisation.\n\n"
            "Ce que j'apporte à votre projet :\n"
            "• Conception et développement d'applications web full stack : backend Python/FastAPI, "
            "frontend React (TypeScript), données Google BigQuery.\n"
            "• Déploiement et intégration continue sur GCP (Cloud Run, Cloud Functions, GKE), "
            "avec une approche DevSecOps.\n"
            "• Revues de code, tests automatisés, bonnes pratiques — j'apporte de la rigueur "
            "technique aux équipes projet.\n\n"
            "Stack maîtrisée : Python, FastAPI, React, GCP/BigQuery, Docker, PostgreSQL, CI/CD.\n\n"
            "Disponible pour échanger sur votre contexte et les attendus du projet.\n\n"
            "Cordialement,\nDan Saffar\n"
            "Portfolio : https://lutece.ia"
        ),
        "draft_model": "fallback-hermes-agent"
    },
]

def main():
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    updated = 0
    for offer in OFFERS_TO_DRAFT:
        source = offer["source"]
        ext_id = offer["external_id"]
        title = offer["title"]
        notes = {
            "external_id": ext_id,
            "url": offer["url"],
            "title": title,
            "company": offer.get("company", ""),
            "source": source,
            "fit_score": offer["fit_score"],
            "fit_reasons": offer["fit_reasons"],
            "draft_body": offer["draft_body"],
            "draft_model": offer["draft_model"],
        }

        existing = conn.execute(
            "SELECT id, notes FROM offers WHERE source = ? AND external_id = ?",
            (source, ext_id)
        ).fetchone()

        if existing:
            old_notes = json.loads(existing["notes"]) if existing["notes"] else {}
            # Preserve existing draft if already set
            if old_notes.get("draft_body"):
                logger.info("  • %s — draft déjà existant, ignoré", title[:50])
                continue
            old_notes.update(notes)
            conn.execute(
                "UPDATE offers SET fit_score = ?, notes = ?, updated_at = ? WHERE id = ?",
                (offer["fit_score"], json.dumps(old_notes, ensure_ascii=False), now, existing["id"]),
            )
        else:
            old_notes = {}
            old_notes.update(notes)
            conn.execute(
                "INSERT INTO offers (source, external_id, title, company, url, status, fit_score, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?)",
                (
                    source, ext_id, title[:500], offer.get("company", ""), offer["url"],
                    offer["fit_score"],
                    json.dumps(old_notes, ensure_ascii=False),
                    now, now,
                ),
            )

        draft_len = len(offer["draft_body"])
        logger.info("  ✓ %s — draft ajouté (%d signes)", title[:50], draft_len)
        updated += 1

    conn.commit()
    conn.close()
    logger.info("Fallback drafts: %d offre(s) mise(s) à jour", updated)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())