"""Livraison Telegram : decouper, reessayer, et ne mentir sur rien.

Ce module existe a cause d'une panne silencieuse. Un email portant 38
documents produisait un resume de plusieurs milliers de caracteres ;
`send_message` levait alors `message is too long`, l'exception etait
absorbee par la boucle de fond, et le client ne recevait RIEN - alors
que Sheets et Drive venaient d'etre modifies. Aucune alerte, aucun
message, et une comptabilite qui avait bouge.

Trois regles, dans cet ordre :

  1. un message trop long est DECOUPE, jamais tronque : perdre la fin
     d'un resume comptable serait aussi grave que ne rien envoyer ;
  2. un refus temporaire (429) est RESPECTE - on attend le delai que
     Telegram indique, on ne le devine pas ;
  3. un envoi n'est repute fait que lorsque Telegram l'a CONFIRME. Sans
     confirmation, l'appelant ne doit rien marquer comme notifie, pour
     que le cycle suivant reessaie.

Rien de ce qui est journalise ici ne porte de secret : un identifiant de
conversation, une taille, un numero de tentative.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("demo_bot.telegram")

# Telegram accepte 4096 caracteres. On s'arrete a 3500 : la marge absorbe
# les entites de formatage et les caracteres multi-octets, qui comptent
# double dans certaines mesures cote serveur.
MAX_CHARS = 3500

# Nombre total de tentatives par morceau, reessais compris.
MAX_ATTEMPTS = 3

# Plafond d'attente sur un 429. Au-dela, mieux vaut rendre la main et
# reessayer au cycle suivant que bloquer la boucle Gmail plusieurs minutes.
MAX_RETRY_AFTER = 60


class TelegramDeliveryError(RuntimeError):
    """L'envoi n'a pas abouti. L'appelant NE DOIT PAS marquer notifie."""


def split_message(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Decoupe un texte en morceaux envoyables, sans couper un mot.

    On coupe d'abord aux fins de LIGNE : un resume comptable se lit en
    lignes, et couper au milieu d'un montant le rendrait faux a l'oeil.
    Une ligne isolee plus longue que la limite est coupee aux espaces, et
    seulement en dernier recours au caractere - un mot de 3500 caracteres
    n'existe pas dans un resume, mais un fichier peut en porter le nom.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    morceaux: list[str] = []
    courant = ""
    for ligne in text.split("\n"):
        for fragment in _fit(ligne, limit):
            candidat = f"{courant}\n{fragment}" if courant else fragment
            if len(candidat) <= limit:
                courant = candidat
            else:
                if courant:
                    morceaux.append(courant)
                courant = fragment
    if courant:
        morceaux.append(courant)
    return morceaux


def _fit(ligne: str, limit: int) -> list[str]:
    """Reduit UNE ligne a des fragments qui tiennent dans la limite."""
    if len(ligne) <= limit:
        return [ligne]
    fragments: list[str] = []
    reste = ligne
    while len(reste) > limit:
        coupe = reste.rfind(" ", 0, limit)
        if coupe <= 0:
            coupe = limit          # aucun espace : on coupe au caractere
        fragments.append(reste[:coupe].rstrip())
        reste = reste[coupe:].lstrip()
    if reste:
        fragments.append(reste)
    return fragments


def _retry_after(exc: Exception) -> int | None:
    """Delai impose par Telegram, ou None si l'erreur n'est pas un 429.

    Reconnu par ATTRIBUT et non par classe : cela evite d'importer aiogram
    ici, donc de rendre le test de la temporisation dependant de la
    bibliotheque. `TelegramRetryAfter` porte bien `retry_after`.
    """
    valeur = getattr(exc, "retry_after", None)
    if valeur is None:
        return None
    try:
        return max(0, int(valeur))
    except (TypeError, ValueError):
        return None


async def send_text(
    send: Callable[..., Awaitable[Any]],
    chat_id: int,
    text: str,
    *,
    limit: int = MAX_CHARS,
    max_attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    label: str = "message",
) -> list[int]:
    """Envoie un texte, decoupe si besoin. Leve si un morceau n'aboutit pas.

    Rend la liste des `message_id` confirmes par Telegram. L'appelant s'en
    sert comme preuve d'envoi : c'est la seule chose qui autorise a
    marquer un document comme notifie.
    """
    pause = sleep or asyncio.sleep
    morceaux = split_message(text, limit)
    envoyes: list[int] = []

    for numero, morceau in enumerate(morceaux, start=1):
        for tentative in range(1, max_attempts + 1):
            logger.info(
                "Telegram envoi | chat=%s | %s | morceau=%d/%d | taille=%d | "
                "tentative=%d/%d",
                chat_id, label, numero, len(morceaux), len(morceau),
                tentative, max_attempts,
            )
            try:
                envoi = await send(chat_id=chat_id, text=morceau)
            except Exception as exc:  # noqa: BLE001 - on trie juste apres
                delai = _retry_after(exc)
                if delai is not None and tentative < max_attempts:
                    if delai > MAX_RETRY_AFTER:
                        logger.warning(
                            "Telegram 429 | chat=%s | delai demande %ss superieur "
                            "au plafond %ss : abandon, reessai au cycle suivant",
                            chat_id, delai, MAX_RETRY_AFTER,
                        )
                        raise TelegramDeliveryError(
                            f"429 avec retry_after={delai}s"
                        ) from exc
                    logger.warning(
                        "Telegram 429 | chat=%s | attente de %ss avant nouvelle "
                        "tentative (%d/%d)", chat_id, delai, tentative, max_attempts,
                    )
                    await pause(delai + 1)
                    continue
                logger.error(
                    "Telegram echec | chat=%s | %s | morceau=%d/%d | "
                    "tentative=%d/%d | %s",
                    chat_id, label, numero, len(morceaux), tentative,
                    max_attempts, type(exc).__name__,
                )
                raise TelegramDeliveryError(
                    f"envoi impossible ({type(exc).__name__})"
                ) from exc
            identifiant = int(getattr(envoi, "message_id", 0) or 0)
            envoyes.append(identifiant)
            logger.info(
                "Telegram confirme | chat=%s | %s | morceau=%d/%d | message_id=%s",
                chat_id, label, numero, len(morceaux), identifiant,
            )
            break
        else:
            raise TelegramDeliveryError(
                f"{max_attempts} tentatives epuisees sur le morceau {numero}"
            )
    return envoyes
