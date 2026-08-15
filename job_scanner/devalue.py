"""
devalue.py — Décodeur du format de sérialisation `devalue` (payloads Nuxt 3).

Nuxt 3 embarque l'état de la page dans `<script id="__NUXT_DATA__">` sous forme
de **tableau plat** : chaque entrée est une valeur, et tout entier rencontré à
l'intérieur d'un objet ou d'un tableau est un *index* vers une autre entrée, pas
un nombre. Ce format déduplique les valeurs partagées et autorise les cycles.

Exemple :
    [{"title": 1, "id": 2}, "Architecte IA", 644769]
    →  {"title": "Architecte IA", "id": 644769}

Lire ce payload évite toute automatisation de navigateur : une requête HTTP
suffit à récupérer des données déjà structurées.

Le module est volontairement pur (aucune I/O réseau) pour rester testable.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List

__all__ = ["DevalueError", "parse_devalue", "extract_nuxt_payload"]


class DevalueError(ValueError):
    """Payload devalue absent, tronqué ou malformé."""


# Index négatifs réservés par devalue pour les valeurs non représentables en JSON.
_SENTINELS: Dict[int, Any] = {
    -1: None,           # undefined
    -2: None,           # trou de tableau creux
    -3: math.nan,
    -4: math.inf,
    -5: -math.inf,
    -6: -0.0,
}

# Enveloppes Nuxt sans portée sémantique côté Python : on rend la valeur interne.
_TRANSPARENT_TAGS = frozenset(
    {"Reactive", "Ref", "ShallowRef", "ShallowReactive", "EmptyRef", "EmptyShallowRef"}
)

_NUXT_SCRIPT_RE = re.compile(
    r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def parse_devalue(flat: Any) -> Any:
    """
    Reconstruit la valeur racine d'un tableau plat devalue.

    Args:
        flat: le tableau tel que sérialisé par Nuxt. La racine est à l'index 0.

    Returns:
        Les données hydratées (dicts, listes, scalaires Python).

    Raises:
        DevalueError: payload vide, non-liste, ou référence hors bornes.
    """
    if not isinstance(flat, list):
        raise DevalueError(f"payload devalue attendu sous forme de liste, reçu {type(flat).__name__}")
    if not flat:
        raise DevalueError("payload devalue vide")

    # Mémoïsation par index : assure le partage d'instances et coupe les cycles.
    resolved: Dict[int, Any] = {}

    def hydrate(index: int) -> Any:
        if index in _SENTINELS:
            return _SENTINELS[index]
        if index in resolved:
            return resolved[index]
        if not isinstance(index, int) or not 0 <= index < len(flat):
            raise DevalueError(f"référence devalue hors bornes : {index!r}")

        value = flat[index]

        if isinstance(value, list):
            return _hydrate_list(index, value, hydrate, resolved)
        if isinstance(value, dict):
            container: Dict[str, Any] = {}
            resolved[index] = container  # publier avant de descendre → cycles gérés
            for key, ref in value.items():
                container[key] = hydrate(ref)
            return container

        # Scalaire (str, bool, None, int, float) : valeur littérale.
        resolved[index] = value
        return value

    return hydrate(0)


def _hydrate_list(index: int, value: List[Any], hydrate, resolved: Dict[int, Any]) -> Any:
    """Résout une entrée liste, qui peut être un tableau ou une valeur taguée."""
    tag = value[0] if value and isinstance(value[0], str) else None

    if tag == "Date":
        resolved[index] = hydrate(value[1])
        return resolved[index]
    if tag == "Set":
        items: List[Any] = []
        resolved[index] = items
        items.extend(hydrate(ref) for ref in value[1:])
        return items
    if tag == "Map":
        mapping: Dict[Any, Any] = {}
        resolved[index] = mapping
        for position in range(1, len(value) - 1, 2):
            mapping[hydrate(value[position])] = hydrate(value[position + 1])
        return mapping
    if tag in _TRANSPARENT_TAGS:
        resolved[index] = hydrate(value[1])
        return resolved[index]

    array: List[Any] = []
    resolved[index] = array
    array.extend(hydrate(ref) for ref in value)
    return array


def extract_nuxt_payload(html: str) -> Any:
    """
    Extrait puis décode le payload `__NUXT_DATA__` d'une page Nuxt 3.

    Raises:
        DevalueError: balise absente ou JSON invalide.
    """
    match = _NUXT_SCRIPT_RE.search(html or "")
    if not match:
        raise DevalueError("balise <script id=\"__NUXT_DATA__\"> introuvable dans la page")

    try:
        flat = json.loads(match.group(1).strip())
    except json.JSONDecodeError as exc:
        raise DevalueError(f"payload __NUXT_DATA__ illisible : {exc}") from exc

    return parse_devalue(flat)
