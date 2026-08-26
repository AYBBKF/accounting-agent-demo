"""Idempotence des NOTIFICATIONS Telegram.

Le pipeline etait deja idempotent cote donnees : aucun doublon n'apparaissait
dans Sheets, Drive ou Calendar. Ce qui ne l'etait pas, c'etait la PAROLE du
bot. A chaque cycle Gmail il reannoncait ce qu'il venait de recalculer :
  - "deja importe" pour chaque document termine ;
  - "ignore" pour chaque .txt ou .csv du ZIP ;
  - une demande de validation pour CI-2026-045 et EXP-2026-019.

Ce module prouve la regle demandee : un message UNIQUEMENT lors d'une
transition reelle d'etat, zero message quand rien ne change.
"""
import pytest

from app import doc_store as store
from app.db import init_db
from app.doc_extract import extract_document
from app.doc_policy import ACTION_AUTO, ACTION_REVIEW
from app.mail_worker import (
    NOTIFY_COMPLETED,
    NOTIFY_WAITING,
    build_summary,
    notify_state_of,
)
from test_mail_worker import ACHAT, FakeMailWorker, pdf_bytes, text_of, zip_of
from test_validation_policy import ANOMALIE, PACK_MEMBERS, RotatingGmail, SANS_ICE, VENTE
from workbook_fake import FakeWorkbook

# Un pack realiste : quatre PDF + les fichiers d'accompagnement qu'un cabinet
# met toujours dans une archive comptable.
PACK_AVEC_ANNEXES = {
    **PACK_MEMBERS,
    "pack/README.txt": b"Pack de test comptable X BLASTE",
    "pack/manifeste.csv": b"fichier;montant" + bytes([10]) + b"achat.pdf;7500",
    "pack/SHA256SUMS": b"aaaa  01_FACTURE_ACHAT.pdf",
}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "demo.db")
    init_db(path)
    store.ensure_schema(path)
    return path


@pytest.fixture
def registry(monkeypatch):
    import app.doc_pipeline as module

    table: dict[bytes, str] = {}
    for name in (ACHAT, ANOMALIE, SANS_ICE, VENTE):
        table[pdf_bytes(name)] = text_of(name)

    def fake_read(content, company="X BLASTE", ocr=True):
        if content not in table:
            raise ValueError("PDF illisible")
        return extract_document([table[content]], company=company)

    monkeypatch.setattr(module, "extract_from_pdf_bytes", fake_read)
    return table


@pytest.fixture
def worker(db_path, registry):
    w = RotatingGmail(FakeWorkbook(), db_path)
    w.add_message(
        "m-pack", internal_date=w.moment(),
        attachments={"Pack_test_comptable.zip": zip_of(PACK_AVEC_ANNEXES)},
    )
    return w


def deliver(worker) -> list[str]:
    """Un cycle complet, exactement comme la boucle du bot le fait.

    Renvoie la liste des messages Telegram REELLEMENT envoyes. L'etat notifie
    n'est enregistre qu'apres l'envoi, comme en production.
    """
    envoyes: list[str] = []
    for summary in worker.process_once():
        if not summary.should_notify:
            continue
        envoyes.append(build_summary(summary))
        for outcome in summary.to_review:
            envoyes.append(f"VALIDATION {outcome.filename}")
            worker.mark_notified(outcome, telegram_message_id=1000 + len(envoyes))
        for outcome in summary.notified_outcomes:
            if outcome not in summary.to_review:
                worker.mark_notified(outcome)
    return envoyes


# === 1. cinq cycles consecutifs sur le meme ZIP ===========================

def test_five_consecutive_cycles_notify_only_on_the_first(worker):
    premier = deliver(worker)
    assert premier, "le premier passage doit evidemment parler"

    for tour in range(2, 6):
        assert deliver(worker) == [], f"cycle {tour} : le bot doit se taire"


def test_a_cycle_without_transition_sends_exactly_zero_message(worker):
    deliver(worker)
    summaries = worker.process_once()
    assert all(not s.should_notify for s in summaries)
    assert sum(len(s.notified_outcomes) for s in summaries) == 0


# === 2. redemarrage du worker ============================================

def test_restarting_the_worker_sends_nothing_again(db_path, registry):
    """L'etat notifie vit dans SQLite, pas en memoire."""
    premier = RotatingGmail(FakeWorkbook(), db_path)
    premier.add_message(
        "m-pack", internal_date=premier.moment(),
        attachments={"Pack_test_comptable.zip": zip_of(PACK_AVEC_ANNEXES)},
    )
    assert deliver(premier)

    # Nouveau processus : meme volume SQLite, meme coffre, objets neufs.
    second = RotatingGmail(FakeWorkbook(), db_path)
    second.messages = premier.messages
    second.blobs = premier.blobs
    assert deliver(second) == []


# === 3. une validation en attente reste cliquable, sans etre renvoyee =====

def test_a_pending_validation_is_never_resent_but_stays_clickable(worker):
    deliver(worker)
    en_attente = store.list_pending_review(worker._db_path, worker._chat_id)
    assert en_attente, "le pack contient bien des documents a valider"

    for _ in range(3):
        assert deliver(worker) == []

    for row in en_attente:
        fiche = store.get_document(worker._db_path, row["doc_key"])
        assert fiche["state"] == store.NEEDS_REVIEW
        assert fiche["last_notified_state"] == NOTIFY_WAITING
        assert fiche["validation_notification_sent_at"]
        assert fiche["telegram_message_id"]
        # Le bouton reste vivant : la cle courte retrouve toujours la fiche.
        vivant = store.find_by_key_prefix(
            worker._db_path, worker._chat_id, row["doc_key"][:24]
        )
        assert vivant is not None and vivant["doc_key"] == row["doc_key"]


# === 4. /resend_pending renvoie exactement une fois =======================

def test_resend_pending_sends_each_waiting_document_once(worker):
    deliver(worker)
    attente = worker.pending_validations()
    assert attente, "il reste des documents en attente de decision"
    noms = sorted(o.filename for o in attente)
    assert len(noms) == len(set(noms)), "aucun doublon dans le renvoi"
    assert all(o.action == ACTION_REVIEW for o in attente)

    # Le renvoi est volontaire : il ne declenche aucune ecriture comptable.
    avant = len(worker.workbook.calls)
    worker.pending_validations()
    ecritures = [
        call for call in worker.workbook.calls[avant:]
        if "UPDATE" in call[0] or "APPEND" in call[0]
    ]
    assert ecritures == []

    # Et le cycle automatique, lui, continue de se taire.
    assert deliver(worker) == []


# === 5. TXT / CSV / README dans un ZIP : zero notification ================

def test_non_pdf_members_of_a_zip_are_ignored_silently(worker):
    messages = deliver(worker)
    entier = " ".join(messages)
    for bruit in ("README.txt", "manifeste.csv", "SHA256SUMS"):
        assert bruit not in entier, f"{bruit} ne doit jamais etre annonce"


def test_non_pdf_members_never_reach_the_accounting_journal(worker):
    deliver(worker)
    journal = worker.workbook.rows("14_IMPORTS_LOG")
    for ligne in journal:
        texte = " ".join(str(c) for c in ligne)
        assert "README" not in texte
        assert "manifeste" not in texte
        assert "SHA256SUMS" not in texte


# === 6. document deja importe : aucune ecriture, aucun message ============

def test_an_already_imported_document_writes_nothing_and_says_nothing(worker):
    deliver(worker)
    avant = len(worker.workbook.calls)
    lignes_avant = {
        onglet: len(worker.workbook.rows(onglet))
        for onglet in ("05_FACTURES_ACHATS", "04_FACTURES_VENTES", "14_IMPORTS_LOG")
    }

    assert deliver(worker) == []

    ecritures = [
        call for call in worker.workbook.calls[avant:]
        if "UPDATE" in call[0] or "APPEND" in call[0] or "CREATE" in call[0]
    ]
    assert ecritures == [], f"aucune ecriture attendue, obtenu {ecritures}"
    for onglet, compte in lignes_avant.items():
        assert len(worker.workbook.rows(onglet)) == compte


# === 7. une vraie transition produit exactement une notification ==========

def test_a_quarantined_document_is_announced_exactly_once(worker):
    """Un document ecarte se signale une fois, puis se tait.

    Il n'y a plus de transition "en attente -> valide" a annoncer : le
    document reste ecarte tant que le comptable ne l'a pas traite. Ce qui
    doit rester vrai, c'est qu'il ne reveille pas le client a chaque
    cycle Gmail.
    """
    premier = deliver(worker)
    assert premier, "la mise a l'ecart doit etre annoncee une fois"

    ligne = store.list_pending_review(worker._db_path, worker._chat_id)[0]
    fiche = store.get_document(worker._db_path, ligne["doc_key"])
    assert fiche["state"] == store.NEEDS_REVIEW

    assert deliver(worker) == []
    assert deliver(worker) == []


def test_a_partial_document_notifies_once_when_it_completes(worker, db_path):
    """partial -> completed est une transition reelle : un seul message."""
    deliver(worker)

    # Un document termine que l'on remet dans l'etat ou une panne l'aurait
    # laisse : ecriture comptable faite, etapes suivantes inachevees. Son
    # dernier etat annonce est donc 'partial'.
    termine = [
        row for row in store.list_documents(db_path, worker._chat_id)
        if row["state"] == store.COMPLETED
    ]
    assert termine, "le pack contient au moins un document importe"
    cle = termine[0]["doc_key"]
    store.set_state(db_path, cle, store.PARTIAL)
    store.mark_notified(db_path, cle, "partial")

    messages = deliver(worker)
    assert len(messages) >= 1, "la transition partial -> completed doit parler"
    assert store.get_document(db_path, cle)["state"] == store.COMPLETED
    assert store.get_document(db_path, cle)["last_notified_state"] == NOTIFY_COMPLETED
    assert deliver(worker) == [], "et une seule fois"


# === 8. aucun doublon Sheets / Drive / Calendar ===========================

def test_five_cycles_create_no_duplicate_anywhere(worker):
    deliver(worker)
    reference = {
        onglet: len(worker.workbook.rows(onglet))
        for onglet in (
            "04_FACTURES_VENTES", "05_FACTURES_ACHATS", "14_IMPORTS_LOG",
            "17_AVOIRS", "18_DOCUMENTS_COMMERCIAUX", "19_ECHEANCES_A_PAYER",
        )
    }
    fichiers = len(worker.workbook.drive_files)
    evenements = len(worker.workbook.calendar_events)

    for _ in range(4):
        deliver(worker)

    for onglet, compte in reference.items():
        assert len(worker.workbook.rows(onglet)) == compte, onglet
    assert len(worker.workbook.drive_files) == fichiers
    assert len(worker.workbook.calendar_events) == evenements


# === garde-fous sur la regle elle-meme ====================================

def test_notify_state_maps_actions_to_states():
    from app.doc_pipeline import DocumentOutcome

    assert notify_state_of(DocumentOutcome(doc_key="k", filename="f",
                                           action=ACTION_AUTO)) == NOTIFY_COMPLETED
    assert notify_state_of(DocumentOutcome(doc_key="k", filename="f",
                                           action=ACTION_REVIEW)) == NOTIFY_WAITING


def test_the_migration_adds_the_columns_to_an_existing_database(tmp_path):
    """Base de production existante : les colonnes sont AJOUTEES, sans perte."""
    import sqlite3

    path = str(tmp_path / "ancienne.db")
    init_db(path)
    store.ensure_schema(path)
    with sqlite3.connect(path) as conn:
        for colonne in (
            "last_notified_state", "notified_at",
            "validation_notification_sent_at", "telegram_message_id",
        ):
            conn.execute(f"ALTER TABLE documents DROP COLUMN {colonne}")
        conn.commit()

    store.ensure_schema(path)
    with sqlite3.connect(path) as conn:
        colonnes = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    assert {"last_notified_state", "notified_at",
            "validation_notification_sent_at", "telegram_message_id"} <= colonnes
