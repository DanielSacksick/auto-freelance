"""
telegram_sender.py — Envoi de la notification directement via l'API Telegram.

Envoi direct plutôt que via une couche de présentation tierce : c'est ce qui
garantit qu'un bloc `<pre>` arrive tel quel. Vérifié par le retour de l'API :
Telegram répond bien `entities: ['pre']`, condition pour que le bouton
« Copier » apparaisse sur le bloc de candidature.

Un tap sur le bloc copie la candidature. C'est le seul chemin vers le
presse-papier qui fonctionne aussi bien depuis un ordinateur que depuis un
téléphone, et qui ne réveille aucun agent.

Le bouton « Postuler » est un lien : Telegram l'ouvre sans rien renvoyer au
serveur, donc sans latence ni jetons consommés.
"""

from __future__ import annotations

import html
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram plafonne un message à 4096 caractères ; on garde de la marge pour
# l'en-tête, les échappements HTML et les retours à la ligne.
MAX_BODY_CHARS = 3200

# Telegram plafonne callback_data à 64 octets. « applied: » + un UUID tient.
MAX_CALLBACK_BYTES = 64


class TelegramSendError(RuntimeError):
    """L'API Telegram a refusé le message."""


def build_message(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Compose le message d'une mission : en-tête, conditions, candidature, boutons.

    Returns:
        Le corps prêt pour `sendMessage`, ou None si la ligne est inexploitable.
    """
    opportunity_id = item.get("id")
    body = (item.get("draft_body") or "").strip()
    if not opportunity_id or not body:
        return None

    title = item.get("title") or "Mission"
    score = item.get("fit_score")
    header = f"<b>{html.escape(title[:90])}</b>"
    if score is not None:
        header = f"[{float(score):.0f}/100] {header}"

    context = " · ".join(bit for bit in (_rate(item), item.get("company"), _conditions(item)) if bit)

    text = header
    if context:
        text += f"\n<i>{html.escape(context)}</i>"
    text += f"\n\n<pre>{html.escape(_truncate(body))}</pre>\n"
    text += "\n👆 Tape le texte pour le copier"

    return {
        "parse_mode": "HTML",
        "text": text,
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [_buttons(str(opportunity_id), item.get("url", ""))]},
    }


def send_briefing(token: str, chat_id: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Envoie une mission par message.

    Un échec sur une mission n'interrompt pas les suivantes : mieux vaut quatre
    candidatures sur cinq qu'un briefing perdu en entier.
    """
    sent, failed = [], []

    for item in items:
        payload = build_message(item)
        if payload is None:
            failed.append({"id": item.get("id"), "error": "message inexploitable"})
            continue
        try:
            message_id = _post(token, chat_id, payload)
            sent.append({"id": item.get("id"), "message_id": message_id})
        except Exception as exc:
            logger.warning("telegram: envoi impossible pour %s (%s)", item.get("id"), exc)
            failed.append({"id": item.get("id"), "error": str(exc)})

    return {"sent": len(sent), "failed": len(failed), "details": {"sent": sent, "failed": failed}}


def send_text(token: str, chat_id: str, text: str) -> Optional[int]:
    """Envoie un message simple (rappel de pipeline, absence de résultat)."""
    try:
        return _post(token, chat_id, {"text": text, "parse_mode": "HTML",
                                      "disable_web_page_preview": True})
    except Exception as exc:
        logger.warning("telegram: message simple non envoyé (%s)", exc)
        return None


# -- Interne -------------------------------------------------------------


def _buttons(opportunity_id: str, url: str) -> List[Dict[str, Any]]:
    """« Postuler » est un lien pur : aucun aller-retour serveur au clic."""
    buttons: List[Dict[str, Any]] = []
    if url:
        buttons.append({"text": "📄 Postuler", "url": url})
    buttons.append({"text": "✅ Fait", "callback_data": _callback(f"applied:{opportunity_id}")})
    buttons.append({"text": "❌ Passer", "callback_data": _callback(f"skip:{opportunity_id}")})
    return buttons


def _callback(value: str) -> str:
    """Tronque au plafond Telegram plutôt que de laisser l'API refuser l'envoi."""
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_CALLBACK_BYTES:
        return value
    return encoded[:MAX_CALLBACK_BYTES].decode("utf-8", errors="ignore")


def _post(token: str, chat_id: str, payload: Dict[str, Any]) -> int:
    body = json.dumps({"chat_id": chat_id, **payload}).encode("utf-8")
    request = urllib.request.Request(
        API_ROOT.format(token=token), data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise TelegramSendError(f"HTTP {exc.code}: {exc.read()[:200]!r}") from exc

    if not result.get("ok"):
        raise TelegramSendError(str(result)[:200])
    return result["result"]["message_id"]


def _rate(item: Dict[str, Any]) -> str:
    low, high = item.get("daily_rate_min"), item.get("daily_rate_max")
    currency = item.get("currency") or "EUR"
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency, currency)
    if not low and not high:
        return "TJM non communiqué"
    if low and high and low != high:
        return f"{low}-{high} {symbol}/j"
    return f"{low or high} {symbol}/j"


def _conditions(item: Dict[str, Any]) -> str:
    bits = []
    if item.get("duration_months"):
        bits.append(f"{item['duration_months']} mois")
    if item.get("remote_mode"):
        bits.append(f"remote {item['remote_mode']}")
    if item.get("location"):
        bits.append(str(item["location"])[:40])
    return " · ".join(bits)


def _truncate(body: str) -> str:
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[: MAX_BODY_CHARS - 1].rsplit(" ", 1)[0] + "…"
