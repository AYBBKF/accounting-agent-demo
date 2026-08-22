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

import pytest

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


# === noms d'arguments Composio : le defaut que le double masquait ==========

def test_the_drive_calls_use_the_argument_names_composio_expects():
    """Regression : les trois appels Drive partaient avec de mauvais noms.

    `GOOGLEDRIVE_UPLOAD_FROM_URL` recevait `file_url`/`file_name`/`folder_id`
    au lieu de `source_url`/`name`/`parent_folder_id`, et echouait donc a
    CHAQUE document en production. Pire, `GOOGLEDRIVE_FIND_FOLDER` recevait
    `folder_name`, un champ qu'il ignore : la recherche n'etait bornee ni par
    le nom ni par le parent, et renvoyait un dossier arbitraire du Drive.
    """
    from workbook_fake import TOOL_ARGUMENTS, check_arguments

    required_upload, optional_upload = TOOL_ARGUMENTS["GOOGLEDRIVE_UPLOAD_FROM_URL"]
    assert "source_url" in required_upload and "name" in required_upload
    assert "parent_folder_id" in optional_upload
    for banned in ("file_url", "file_name", "folder_id"):
        assert banned not in required_upload | optional_upload

    with pytest.raises(AssertionError, match="argument"):
        check_arguments("GOOGLEDRIVE_UPLOAD_FROM_URL",
                        {"file_url": "https://x/y.pdf", "file_name": "y.pdf"})
    with pytest.raises(AssertionError, match="argument"):
        check_arguments("GOOGLEDRIVE_FIND_FOLDER", {"folder_name": "Factures"})


def test_a_folder_search_is_always_bounded_by_name_and_parent(pipeline_for_drive):
    """Un dossier ne doit jamais etre 'trouve' au hasard."""
    pipeline, workbook = pipeline_for_drive
    pipeline.ensure_folder("Racine")
    pipeline.ensure_folder("Factures achats", "folder-1")

    searches = [a for s, a in workbook.calls if s == "GOOGLEDRIVE_FIND_FOLDER"]
    assert searches, "aucune recherche de dossier"
    for query in searches:
        assert query.get("name_exact"), "recherche sans nom exact"
    assert searches[-1]["parent_folder_id"] == "folder-1"


def test_the_calendar_reminder_carries_an_instant_not_a_date():
    """Une date seule ('2026-08-31') est rejetee par l'API Calendar."""
    from workbook_fake import check_arguments

    with pytest.raises(AssertionError, match="instant ISO"):
        check_arguments("GOOGLECALENDAR_CREATE_EVENT", {"start_datetime": "2026-08-31"})
    check_arguments("GOOGLECALENDAR_CREATE_EVENT",
                    {"start_datetime": "2026-08-31T09:00:00", "timezone": "Africa/Casablanca"})


@pytest.fixture
def pipeline_for_drive():
    """Pipeline branche sur un faux classeur, pour observer les appels Drive."""
    from app import doc_store as store
    from app.doc_pipeline import DocumentPipeline

    path = tempfile.mktemp(suffix=".db")
    init_db(path)
    store.ensure_schema(path)
    workbook = FakeWorkbook()
    try:
        yield DocumentPipeline(
            workbook, db_path=path, chat_id=999653395, spreadsheet_id="sheet-test"
        ), workbook
    finally:
        Path(path).unlink(missing_ok=True)
