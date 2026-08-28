"""Un message envoye est un message CONFIRME, ou il n'a pas eu lieu.

Panne d'origine : un email de 38 documents produisait un resume de
plusieurs milliers de caracteres. Telegram le refusait, l'exception etait
absorbee par la boucle de fond, et le client ne recevait rien - alors que
Sheets et Drive venaient d'etre modifies.

Ce module verifie les trois proprietes qui empechent que cela se
reproduise, et surtout la troisieme : un echec ne doit JAMAIS ressembler
a un succes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app.telegram_delivery import (
    MAX_CHARS,
    TelegramDeliveryError,
    send_text,
    split_message,
)


class Envoi:
    """Ce que Telegram renvoie quand il a REELLEMENT accepte le message."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class TropDeRequetes(Exception):
    """Reproduit `TelegramRetryAfter` sans dependre d'aiogram."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Flood control")
        self.retry_after = retry_after


class TropLong(Exception):
    """Reproduit `TelegramBadRequest: message is too long`."""


# === 1. le decoupage =====================================================

def test_a_short_message_is_left_alone():
    assert split_message("bonjour") == ["bonjour"]
    assert split_message("") == []


def test_a_long_message_is_split_under_the_limit():
    texte = "\n".join(f"Facture FAC-2026-{i:04d} : 7 500,00 MAD" for i in range(400))
    morceaux = split_message(texte)

    assert len(morceaux) > 1
    assert all(len(m) <= MAX_CHARS for m in morceaux)


def test_splitting_never_loses_a_single_line():
    """Un resume comptable tronque serait pire qu'un resume absent."""
    lignes = [f"ligne {i}" for i in range(1200)]
    morceaux = split_message("\n".join(lignes))
    recompose = "\n".join(morceaux)
    for ligne in lignes:
        assert ligne in recompose


def test_a_line_is_never_cut_in_the_middle_of_a_word():
    texte = "\n".join(["MONTANT 12 345,67 MAD"] * 400)
    for morceau in split_message(texte):
        assert not morceau.endswith("MONTAN")
        assert not morceau.startswith("T 12")


def test_an_oversized_single_line_is_still_delivered():
    """Un nom de fichier de 8000 caracteres ne doit pas bloquer l'envoi."""
    morceaux = split_message("A" * 8000)
    assert all(len(m) <= MAX_CHARS for m in morceaux)
    assert sum(len(m) for m in morceaux) == 8000


# === 2. l'envoi et le 429 ================================================

@pytest.mark.asyncio
async def test_every_chunk_is_sent_and_confirmed():
    envoyes = []

    async def send(*, chat_id, text):
        envoyes.append(text)
        return Envoi(100 + len(envoyes))

    texte = "\n".join(f"ligne {i}" for i in range(1000))
    identifiants = await send_text(send, 42, texte)

    assert len(identifiants) == len(envoyes) > 1
    assert identifiants == [100 + i for i in range(1, len(envoyes) + 1)]


@pytest.mark.asyncio
async def test_a_429_is_retried_after_the_delay_telegram_asks_for():
    """On respecte le delai annonce ; on ne le devine pas."""
    attentes: list[float] = []
    tentatives = {"n": 0}

    async def send(*, chat_id, text):
        tentatives["n"] += 1
        if tentatives["n"] == 1:
            raise TropDeRequetes(retry_after=3)
        return Envoi(7)

    async def dormir(delai):
        attentes.append(delai)

    identifiants = await send_text(send, 42, "court", sleep=dormir)

    assert identifiants == [7]
    assert attentes == [4]           # le delai demande, plus une seconde


@pytest.mark.asyncio
async def test_an_absurd_retry_after_gives_up_instead_of_blocking_the_loop():
    """Bloquer la boucle Gmail dix minutes serait une panne, pas une attente."""
    async def send(*, chat_id, text):
        raise TropDeRequetes(retry_after=600)

    async def dormir(delai):
        raise AssertionError("on ne doit pas attendre dix minutes")

    with pytest.raises(TelegramDeliveryError):
        await send_text(send, 42, "court", sleep=dormir)


@pytest.mark.asyncio
async def test_a_permanent_error_raises_instead_of_pretending_to_have_sent():
    """LE point central : un echec ne doit pas ressembler a un succes."""
    async def send(*, chat_id, text):
        raise TropLong("message is too long")

    with pytest.raises(TelegramDeliveryError):
        await send_text(send, 42, "peu importe")


@pytest.mark.asyncio
async def test_the_attempts_are_bounded():
    tentatives = {"n": 0}

    async def send(*, chat_id, text):
        tentatives["n"] += 1
        raise TropDeRequetes(retry_after=1)

    async def dormir(delai):
        return None

    with pytest.raises(TelegramDeliveryError):
        await send_text(send, 42, "court", sleep=dormir, max_attempts=3)
    assert tentatives["n"] == 3


# === 3. la trace =========================================================

@pytest.mark.asyncio
async def test_each_attempt_and_confirmation_is_logged(caplog):
    import logging

    async def send(*, chat_id, text):
        return Envoi(55)

    with caplog.at_level(logging.INFO, logger="demo_bot.telegram"):
        await send_text(send, 42, "court", label="resume")

    texte = caplog.text
    assert "Telegram envoi" in texte and "tentative=1/3" in texte
    assert "Telegram confirme" in texte and "message_id=55" in texte


@pytest.mark.asyncio
async def test_nothing_confidential_reaches_the_log(caplog):
    """Le journal porte des tailles et des numeros, jamais le contenu."""
    import logging

    async def send(*, chat_id, text):
        return Envoi(55)

    with caplog.at_level(logging.INFO, logger="demo_bot.telegram"):
        await send_text(send, 42, "ICE 003456789000052 - montant 7 500,00 MAD")

    assert "003456789000052" not in caplog.text
    assert "7 500,00" not in caplog.text
