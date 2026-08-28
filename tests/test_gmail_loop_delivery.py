"""Ce qui n'a pas ete delivre ne doit jamais etre marque comme delivre.

Defaut remonte par la revue independante : quand l'envoi du resume
echouait, les documents importes ou classes etaient tout de meme passes a
`mark_notified`. Le cycle suivant les croyait annonces et ne les
renvoyait plus jamais. Sheets et Drive avaient bouge, le client n'avait
rien recu, et plus rien ne le rattrapait.

Ces documents n'ont pas de message a eux : c'est le resume qui les
annonce. Leur etat notifie depend donc d'une preuve explicite -
`summary_delivered` - qui n'est vraie que lorsque TOUS les morceaux du
resume ont ete confirmes par Telegram.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app import bot as module
from app.telegram_delivery import TelegramDeliveryError


class Envoi:
    def __init__(self, message_id: int = 1) -> None:
        self.message_id = message_id


class BotFactice:
    """Telegram, avec un interrupteur de panne."""

    def __init__(self, echouer_sur: str | None = None) -> None:
        self.echouer_sur = echouer_sur
        self.envoyes: list[str] = []

    async def send_message(self, *, chat_id, text):
        if self.echouer_sur and self.echouer_sur in text:
            raise RuntimeError("message is too long")
        self.envoyes.append(text)
        return Envoi(len(self.envoyes))


def Resultat(doc_key: str, action: str = "auto"):
    """Un vrai `DocumentOutcome`, pas une imitation.

    Un faux objet aurait suivi les attributs que `build_summary` consulte
    aujourd'hui, et se serait tu le jour ou elle en consulte un de plus.
    """
    from app.doc_pipeline import DocumentOutcome

    return DocumentOutcome(
        doc_key=doc_key,
        filename=f"{doc_key}.pdf",
        action=action,
        numero=doc_key,
        tab="05_FACTURES_ACHATS" if action == "auto" else "",
        accounting=action == "auto",
    )


class ResumeFactice:
    """Juste ce que `deliver_summary` regarde, et rien de plus."""

    def __init__(self, importes: list[Resultat], a_verifier: list[Resultat]) -> None:
        self.message_id = "email-test"
        self.subject = "Documents"
        self.outcomes = importes + a_verifier
        self.notified_outcomes = importes + a_verifier
        self.to_review = a_verifier
        self.imported = importes
        self.classified: list[Resultat] = []
        self.notified_rejected: list = []
        self.errors: list = []
        self.truncated = 0
        self.silenced = 0
        self.should_notify = True

    def count(self, action: str) -> int:
        return len([o for o in self.outcomes if o.action == action])


@pytest.fixture
def marques(monkeypatch):
    """Enregistre chaque appel a mark_notified, sans toucher a une base."""
    appels: list[str] = []

    def marquer(outcome, telegram_message_id: int = 0):
        appels.append(outcome.doc_key)

    monkeypatch.setattr(module.mail_worker, "mark_notified", marquer)
    monkeypatch.setattr(module.settings, "gmail_watch_chat_id", 42)
    return appels


# === 1. le resume echoue : RIEN n'est marque =============================

@pytest.mark.asyncio
async def test_a_failed_summary_never_marks_the_imported_documents(marques):
    """LE defaut. Ces documents n'ont ete annonces nulle part."""
    resume = ResumeFactice([Resultat("importe-1"), Resultat("importe-2")], [])
    bot = BotFactice(echouer_sur="Email traite")

    rapport = await module.deliver_summary(bot, resume)

    assert rapport["summary_delivered"] is False
    assert marques == []
    assert bot.envoyes == []


@pytest.mark.asyncio
async def test_a_failed_summary_still_lets_review_documents_through(marques):
    """Un document ecarte porte SA preuve : il ne depend pas du resume."""
    a_verifier = Resultat("a-verifier-1", action="review")
    resume = ResumeFactice([Resultat("importe-1")], [a_verifier])
    bot = BotFactice(echouer_sur="Email traite")

    rapport = await module.deliver_summary(bot, resume)

    assert rapport["summary_delivered"] is False
    assert marques == ["a-verifier-1"]          # lui seul, et pas l'importe
    assert "importe-1" not in marques


# === 2. le cycle suivant reessaie, puis marque ===========================

@pytest.mark.asyncio
async def test_the_next_cycle_retries_and_only_then_marks(marques):
    """Sequence complete : echec, aucun marquage, puis succes reel."""
    resume = ResumeFactice([Resultat("importe-1"), Resultat("importe-2")], [])

    # Cycle 1 : Telegram refuse le resume.
    premier = await module.deliver_summary(BotFactice(echouer_sur="Email traite"), resume)
    assert premier["summary_delivered"] is False
    assert marques == []

    # Cycle 2 : Telegram accepte. C'est SEULEMENT maintenant qu'on marque.
    bot = BotFactice()
    second = await module.deliver_summary(bot, resume)

    assert second["summary_delivered"] is True
    assert marques == ["importe-1", "importe-2"]
    assert bot.envoyes                            # le resume est bien parti


@pytest.mark.asyncio
async def test_a_successful_summary_marks_everything_once(marques):
    a_verifier = Resultat("a-verifier-1", action="review")
    resume = ResumeFactice([Resultat("importe-1")], [a_verifier])

    rapport = await module.deliver_summary(BotFactice(), resume)

    assert rapport["summary_delivered"] is True
    assert sorted(marques) == ["a-verifier-1", "importe-1"]
    assert len(marques) == len(set(marques))      # jamais deux fois


# === 3. le detail des echecs reste exploitable ===========================

@pytest.mark.asyncio
async def test_the_report_names_what_failed(marques):
    resume = ResumeFactice([Resultat("importe-1")], [])
    rapport = await module.deliver_summary(BotFactice(echouer_sur="Email traite"), resume)

    assert [nom for nom, _ in rapport["failed"]] == ["resume"]
    assert rapport["marked"] == []


@pytest.mark.asyncio
async def test_a_summary_that_needs_no_notification_is_skipped(marques):
    resume = ResumeFactice([], [])
    resume.should_notify = False

    rapport = await module.deliver_summary(BotFactice(), resume)

    assert rapport.get("skipped") is True
    assert marques == []


@pytest.mark.asyncio
async def test_one_failed_document_does_not_block_the_others(marques):
    """L'isolement par document, verifie plutot que suppose."""
    bon = Resultat("a-verifier-ok", action="review")
    mauvais = Resultat("a-verifier-ko", action="review")
    resume = ResumeFactice([], [mauvais, bon])
    bot = BotFactice(echouer_sur="a-verifier-ko")

    rapport = await module.deliver_summary(bot, resume)

    assert marques == ["a-verifier-ok"]
    assert [cle for cle, _ in rapport["failed"]] == ["a-verifier-ko"]
