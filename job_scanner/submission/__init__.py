"""
submission — Soumission automatique de candidatures (Playwright).

Le point d'entrée réel est `job_scanner.submission.playwright.submit()`,
piloté par `submit.py` à la racine du dépôt. Ce module de package ne
contient volontairement aucune logique : les sessions vivent dans
`sessions.py`, les mappers de formulaire dans `forms/`.
"""

from __future__ import annotations
