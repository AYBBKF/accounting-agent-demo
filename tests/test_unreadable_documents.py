"""Un PDF illisible se trace UNE fois, et une seule.

Defaut remonte par la revue independante : la deduplication etait
consultee APRES l'extraction. Quand l'extraction levait - PDF corrompu,
scan vide, fichier tronque - ce chemin n'etait jamais atteint. Le meme
fichier illisible renvoye dans un autre email repartait donc de zero, et
laissait une seconde trace pour un seul document physique.

Le controle est desormais fait avant l'extraction, sur l'empreinte du
fichier, qui ne demande aucune lecture du contenu.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app import doc_store as store
from app.doc_policy import ACTION_DUPLICATE, ACTION_REVIEW
from app.review_sheet import TAB_REVIEW

from test_mail_worker import FakeMailWorker, pdf_bytes
from workbook_fake import FakeWorkbook

ILLISIBLE = pdf_bytes("scan-corrompu")


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "demo.db")


@pytest.fixture
def worker(db_path, monkeypatch):
    import app.doc_pipeline as module

    def lecture_impossible(content, company="X BLASTE", ocr=True):
        raise ValueError("PDF illisible : flux xref invalide")

    monkeypatch.setattr(module, "extract_from_pdf_bytes", lecture_impossible)
    return FakeMailWorker(FakeWorkbook(), db_path)


def envoyer(w, message_id: str, offset: int = 0) -> None:
    w.add_message(
        message_id, internal_date=w.moment(offset),
        attachments={"scan.pdf": ILLISIBLE},
    )


# === 1. la premiere arrivee laisse UNE trace =============================

def test_an_unreadable_pdf_leaves_exactly_one_traceable_record(worker):
    envoyer(worker, "email-1")
    resume, _ = worker.process_message("email-1")

    assert len(resume.outcomes) == 1
    resultat = resume.outcomes[0]
    assert resultat.action == ACTION_REVIEW
    assert "illisible" in " ".join(resultat.reasons)

    fiche = store.get_document(worker._db_path, resultat.doc_key)
    assert fiche["state"] == store.FAILED
    assert fiche["error"]


def test_the_unreadable_document_reaches_the_review_tab(worker):
    """Sans ligne rouge, le comptable ignorerait qu'un fichier est arrive."""
    envoyer(worker, "email-1")
    worker.process_message("email-1")

    lignes = worker.workbook.rows(TAB_REVIEW)
    assert len(lignes) == 1
    assert "illisible" in " ".join(str(c) for c in lignes[0])


def test_no_accounting_line_is_ever_written_for_an_unreadable_file(worker):
    envoyer(worker, "email-1")
    worker.process_message("email-1")
    assert worker.workbook.writes_to("05_FACTURES_ACHATS") == []


# === 2. le meme fichier dans un AUTRE email ==============================

def test_the_same_unreadable_pdf_in_another_email_is_attached(worker):
    """Le coeur du defaut : il repartait de zero."""
    envoyer(worker, "email-1")
    premier, _ = worker.process_message("email-1")

    envoyer(worker, "email-2", offset=60)
    second, _ = worker.process_message("email-2")

    assert len(second.outcomes) == 1
    rattache = second.outcomes[0]
    assert rattache.action == ACTION_DUPLICATE

    fiche = store.get_document(worker._db_path, rattache.doc_key)
    assert fiche["superseded_by"] == premier.outcomes[0].doc_key
    assert fiche["state"] == store.SUPERSEDED


def test_the_second_email_adds_no_second_review_row(worker):
    envoyer(worker, "email-1")
    worker.process_message("email-1")
    apres_un = len(worker.workbook.rows(TAB_REVIEW))

    envoyer(worker, "email-2", offset=60)
    worker.process_message("email-2")

    assert len(worker.workbook.rows(TAB_REVIEW)) == apres_un == 1


def test_the_original_record_is_never_deleted(worker):
    envoyer(worker, "email-1")
    worker.process_message("email-1")
    envoyer(worker, "email-2", offset=60)
    worker.process_message("email-2")

    fiches = store.list_documents(worker._db_path, worker._chat_id)
    assert len(fiches) == 2                     # les deux existent toujours
    canoniques = [f for f in fiches if not f.get("superseded_by")]
    assert len(canoniques) == 1


def test_the_second_email_produces_no_telegram_repetition(worker):
    envoyer(worker, "email-1")
    worker.process_message("email-1")

    envoyer(worker, "email-2", offset=60)
    second, _ = worker.process_message("email-2")

    assert second.notifiable == []
    assert second.should_notify is False


def test_rereading_the_same_email_stays_stable(worker):
    """Trois relectures du meme email : toujours une seule ligne."""
    envoyer(worker, "email-1")
    for _ in range(3):
        worker.process_message("email-1")
    assert len(worker.workbook.rows(TAB_REVIEW)) == 1
