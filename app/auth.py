"""Controle d'acces Telegram (liste blanche stricte).

Module isole (sans effet de bord a l'import) pour rester testable
sans instancier un vrai Bot aiogram.
"""
from __future__ import annotations


def is_allowed_telegram_user(user_id: int | None, allowed_ids: set[int]) -> bool:
    """Retourne True uniquement si l'utilisateur figure dans la liste blanche.

    Si la liste blanche est vide, personne n'est autorise (fail-closed) :
    c'est une demo privee, pas un service public.
    """
    if user_id is None:
        return False
    if not allowed_ids:
        return False
    return user_id in allowed_ids
