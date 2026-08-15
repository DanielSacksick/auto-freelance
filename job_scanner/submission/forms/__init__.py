"""
forms/__init__.py — Registre des mappers de formulaire, par plateforme.

Usage ::

    from job_scanner.submission.forms import get_form
    mapper = get_form("freework")
    if mapper:
        mapper.prepare(page)
        mapper.fill_message(page, body)
        ...
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from job_scanner.submission.forms.base import FormMapper

# Registre global : key → FormMapper subclass.
REGISTRY: Dict[str, Any] = {}


def register(cls: Any) -> Any:
    """Enregistre un FormMapper dans le registre global."""
    key = getattr(cls, "key", None)
    if not key:
        raise ValueError(f"FormMapper {cls.__name__} n'a pas de .key")
    REGISTRY[key] = cls
    return cls


def get_form(source: str) -> Optional[FormMapper]:
    """Rend une instance du FormMapper pour la source, ou None si inconnue."""
    cls = REGISTRY.get(source)
    if cls is None:
        return None
    return cls()


def list_platforms() -> Dict[str, str]:
    """Liste les plateformes cartographiées : {key: docstring_titre}."""
    result: Dict[str, Any] = {}
    for key, cls in REGISTRY.items():
        doc = (cls.__doc__ or "").strip().split("\n")[0] if cls.__doc__ else ""
        result[key] = doc[:80]
    return result


# -- Auto-enregistrement ---------------------------------------------------
# Importe les mappers de chaque plateforme pour peupler REGISTRY. Importés ici
# (en bas) et non en tête : `register` doit exister avant que les modules
# plateforme ne l'utilisent au moment de leur import.
from job_scanner.submission.forms import freework  # noqa: E402,F401
from job_scanner.submission.forms import freelance_info  # noqa: E402,F401
from job_scanner.submission.forms import freelancermap  # noqa: E402,F401
from job_scanner.submission.forms import linkedin  # noqa: E402,F401