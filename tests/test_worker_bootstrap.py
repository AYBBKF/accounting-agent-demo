"""Regression : tout premier demarrage du worker sur un volume neuf.

Bug constate EN PRODUCTION, invisible aux tests existants : `process_once`
commencait par la reprise des imports interrompus, qui lit la table
`documents`. Cette table etant creee paresseusement par le pipeline, elle
n'existait pas encore au premier cycle, et le worker mourait a chaque tour
sur "no such table: documents" sans jamais lire un seul email.

Le test n'interroge RIEN avant `process_once` : toucher au curseur creerait
le schema et masquerait exactement le defaut recherche.
"""
import tempfile
import time
from pathlib import Path

from app.db import init_db
from app.doc_extract import extract_document
from app.doc_policy import ACTION_AUTO
from test_mail_worker import ACHAT, FakeMailWorker, pdf_bytes, text_of
from workbook_fake import FakeWorkbook


def test_the_first_cycle_on_a_brand_new_database_creates_its_own_schema(monkeypatch):
    import app.doc_pipeline as module

    monkeypatch.setattr(
        module, "extract_from_pdf_bytes",
        lambda c, company="X BLASTE", ocr=True: extract_document([text_of(ACHAT)]),
    )
    path = tempfile.mktemp(suffix=".db")
    init_db(path)                      # schema du bot seul, sans ensure_schema
    try:
        worker = FakeMailWorker(FakeWorkbook(), path)
        worker.add_message("m-neuf", internal_date=int(time.time()) + 3_600,
                           attachments={"achat.pdf": pdf_bytes(ACHAT)})
        summary = worker.process_once()[0]
        assert summary.outcomes[0].action == ACTION_AUTO
    finally:
        Path(path).unlink(missing_ok=True)
