"""Une reservation liberee ne doit jamais ressusciter un document archive.

Regression observee en production le 23/08/2026 : une validation humaine en
echec appelait `release_document`, qui supprimait la fiche d'un document
DEJA archive dans Drive et DEJA journalise. Le cycle suivant ne le
reconnaissait plus : nouvelle cle d'idempotence, nouvelle copie dans Drive,
nouvelle ligne dans 14_IMPORTS_LOG, nouvelle fiche tiers. Trois lignes en
trop sont apparues dans le journal du client.
"""
import pytest

from app import doc_store as store
from app.db import init_db
from app.doc_extract import extract_document
from test_mail_worker import ACHAT, pdf_bytes, text_of, zip_of
from test_notification_idempotence import PACK_AVEC_ANNEXES, deliver
from test_validation_policy import ANOMALIE, RotatingGmail, SANS_ICE, VENTE
from workbook_fake import FakeWorkbook

CHAT_ID = 999653395


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


def test_a_failed_validation_never_recreates_the_document(worker, db_path):
    deliver(worker)
    attente = store.list_pending_review(db_path, CHAT_ID)
    assert attente
    fiche = attente[0]
    assert fiche["drive_link"], "le document en attente est bien deja archive"

    # L'echec d'ecriture appelle release_document : la fiche doit survivre.
    store.release_document(db_path, fiche["doc_key"])
    toujours_la = store.get_document(db_path, fiche["doc_key"])
    assert toujours_la is not None
    assert toujours_la["state"] == store.NEEDS_REVIEW
    assert toujours_la["drive_link"] == fiche["drive_link"]

    # Et le cycle suivant ne recree donc rien du tout.
    journal_avant = len(worker.workbook.rows("14_IMPORTS_LOG"))
    fichiers_avant = len(worker.workbook.drive_files)
    tiers_avant = len(worker.workbook.rows("03_FOURNISSEURS"))

    assert deliver(worker) == []

    assert len(worker.workbook.rows("14_IMPORTS_LOG")) == journal_avant
    assert len(worker.workbook.drive_files) == fichiers_avant
    assert len(worker.workbook.rows("03_FOURNISSEURS")) == tiers_avant
    assert len(store.list_pending_review(db_path, CHAT_ID)) == len(attente)


def test_release_still_frees_a_document_that_wrote_nothing(db_path):
    """La protection ne doit pas geler une reservation reellement vide."""
    store.claim_document(
        db_path, "cle-vide", CHAT_ID,
        gmail_message_id="m-1", attachment_id="a-1", file_sha256="ff" * 32,
        filename="rien.pdf",
    )
    store.release_document(db_path, "cle-vide")
    assert store.get_document(db_path, "cle-vide") is None
