#!/usr/bin/env python3
"""
notify.py — Notifie les offres prêtes (candidature rédigée, score au-dessus du
seuil) : console (toujours en repli, lisible et copiable dans un terminal) et
Telegram (si `notifications.telegram.enabled` est vrai et que les identifiants
sont présents dans l'environnement).

Ne notifie jamais deux fois la même offre : après un envoi réussi (sur au
moins un canal), l'offre est marquée notifiée via `repo.mark_notified()`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from job_scanner import telegram_sender

logger = logging.getLogger(__name__)


def notify(repo: Any, config: Any, min_score: float) -> Dict[str, Any]:
    """Notifie les offres en attente ; rend un rapport {sent, failed, channels}."""
    offers = repo.offers_pending_notification(min_score=min_score, max_results=10)
    if not offers:
        logger.info("📭 Aucune offre à notifier")
        return {"sent": 0, "failed": 0, "channels": []}

    channels_used: List[str] = []
    delivered_ids: set = set()

    if config.notifications.console:
        _print_console(offers)
        channels_used.append("console")
        delivered_ids.update(str(offer["id"]) for offer in offers)

    if config.notifications.telegram_enabled:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            logger.warning(
                "notify: notifications.telegram.enabled est vrai mais "
                "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID absent de l'environnement "
                "— repli sur la console uniquement"
            )
        else:
            try:
                rows = [_to_telegram_row(offer) for offer in offers]
                report = telegram_sender.send_briefing(token, chat_id, rows)
                sent_ids = {
                    str(entry["id"])
                    for entry in report.get("details", {}).get("sent", [])
                    if entry.get("id")
                }
                if sent_ids:
                    channels_used.append("telegram")
                    delivered_ids.update(sent_ids)
                if report.get("failed"):
                    logger.warning("notify: %d envoi(s) Telegram en échec", report["failed"])
            except Exception as exc:  # jamais faire planter le pipeline pour une notif
                logger.warning("notify: envoi Telegram impossible (%s)", exc)

    if delivered_ids:
        repo.mark_notified(sorted(delivered_ids))

    return {
        "sent": len(delivered_ids),
        "failed": len(offers) - len(delivered_ids),
        "channels": channels_used,
    }


def _print_console(offers: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 60)
    print(f"📬 {len(offers)} offre(s) prête(s) à candidater")
    print("=" * 60)
    for offer in offers:
        score = offer.get("fit_score")
        label = f"[{score:.0f}/100]" if score is not None else "[score inconnu]"
        print(f"\n· {label} {offer.get('title', '?')}")
        print(
            f"  Plateforme : {offer.get('source')}   "
            f"TJM : {_format_rate(offer)}   "
            f"URL : {offer.get('url') or '(voir plateforme)'}"
        )
        print("  --- Candidature (copier-coller) ---")
        print(offer.get("draft_body") or "(pas de brouillon)")
        print("  " + "-" * 40)


def _format_rate(offer: Dict[str, Any]) -> str:
    low, high = offer.get("daily_rate_min"), offer.get("daily_rate_max")
    currency = offer.get("currency") or "EUR"
    if not low and not high:
        return "non communiqué"
    if low and high and low != high:
        return f"{low}-{high} {currency}/j"
    return f"{low or high} {currency}/j"


def _to_telegram_row(offer: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": offer["id"],
        "title": offer.get("title", ""),
        "company": offer.get("company"),
        "url": offer.get("url", ""),
        "fit_score": offer.get("fit_score"),
        "draft_body": offer.get("draft_body", ""),
        "daily_rate_min": offer.get("daily_rate_min"),
        "daily_rate_max": offer.get("daily_rate_max"),
        "currency": offer.get("currency", "EUR"),
        "duration_months": offer.get("duration_months"),
        "remote_mode": offer.get("remote_mode"),
        "location": offer.get("location"),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(
        "notify.py expose notify(repo, config, min_score) ; il attend un repo "
        "SQLite déjà peuplé (voir repo.py) et se lance via pipeline.py, pas "
        "directement."
    )
