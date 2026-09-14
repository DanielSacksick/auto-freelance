#!/usr/bin/env python3
"""
mark_applied.py — Marque une offre comme candidatée depuis l'extérieur du
pipeline (typiquement après une soumission browser manuelle ou via Claude in
Chrome).

Usage :
    python3 scripts/mark_applied.py <source> <external_id> [--proof '{"method": "browser"}']

Exemples :
    python3 scripts/mark_applied.py freework 12345
    python3 scripts/mark_applied.py linkedin 98765 --proof '{"method": "claude-in-chrome", "url": "https://..."}'
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Ajouter la racine du projet au path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from repo import SqliteRepo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Marque une offre comme candidatée (applied) en base."
    )
    parser.add_argument("source", help="Plateforme (freework, linkedin, ...)")
    parser.add_argument("external_id", help="ID externe de l'offre sur la plateforme")
    parser.add_argument(
        "--proof",
        default="{}",
        help='JSON décrivant la preuve de candidature (défaut : "{}")',
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        proof = json.loads(args.proof)
    except json.JSONDecodeError as exc:
        print(f"❌ --proof n'est pas du JSON valide : {exc}", file=sys.stderr)
        return 1

    proof.setdefault("method", "manual")

    repo = SqliteRepo()
    try:
        repo.mark_applied(args.source, args.external_id, proof)
        print(f"✅ {args.source}/{args.external_id} → applied")
        return 0
    finally:
        repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
