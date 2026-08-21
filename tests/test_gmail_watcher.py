"""Tests du worker Gmail : anti-doublon, isolation, et surtout le fait
qu'AUCUNE ecriture Sheets/Drive n'a lieu avant confirmation explicite.

Aucun appel reseau reel : les appels Composio sont mockes.
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.db import get_gmail_message, init_db
from app.gmail_watcher import (
    CALLBACK_CONFIRM_PREFIX,
    CALLBACK_REFUSE_PREFIX,
    GmailWatcher,
    GmailWatcherError,
    build_preview,
)

REAL_PDF_TEXT = (
    Path(__file__).parent / "fixtures" / "facture_test_pdf.txt"
).read_text(encoding="utf-8")

MESSAGE = {
    "messageId": "1a0263e63516623c",
    "threadId": "1a0263e63516623c",
    "subject": "[XBLASTE] Facture test FAC-TEST-2026-001",
    "sender": "Ayoub boukafa <boukafa.ayoub@gmail.com>",
    "messageTimestamp": "2026-08-21T21:33:24Z",
    "attachmentList": [
        {
            "filename": "Facture_test_X_BLASTE_FAC-TEST-2026-001.pdf",
            "mimeType": "application/pdf",
            "attachmentId": "ATT-TOKEN",
        }
    ],
}

TABS = ["00_DASHBOARD", "04_FACTURES_VENTES", "05_FACTURES_ACHATS", "06_RELEVE_BANCAIRE"]


@pytest.fixture
def db_path():
    path = tempfile.mktemp(suffix=".db")
    init_db(path)
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def watcher(db_path):
    return GmailWatcher(
        api_key="cle-de-test", chat_id=999653395, db_path=db_path,
        spreadsheet_id="sheet-de-test",
    )


class Recorder:
    """Enregistre chaque outil Composio appele, pour prouver ce qui a - ou
    surtout n'a PAS - ete execute."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    @property
    def slugs(self) -> list[str]:
        return [s for s, _ in self.calls]

    def __call__(self, slug, arguments):
        self.calls.append((slug, arguments))
        if slug == "GMAIL_FETCH_EMAILS":
            return {"messages": [{"messageId": MESSAGE["messageId"]}]}
        if slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID":
            return MESSAGE
        if slug == "GMAIL_GET_ATTACHMENT":
            return {"file": {"s3url": "https://example.invalid/f.pdf", "mimetype": "application/pdf"}}
        if slug == "GOOGLESHEETS_GET_SPREADSHEET_INFO":
            return {"sheets": [{"properties": {"title": t}} for t in TABS]}
        return {}


def _run_cycle(watcher, recorder):
    from app.invoice_pdf import extract_invoice_fields

    with patch.object(GmailWatcher, "_execute", side_effect=recorder), \
         patch.object(GmailWatcher, "download_attachment", return_value=b"%PDF-fake"), \
         patch("app.gmail_watcher.extract_from_pdf_bytes",
               side_effect=lambda b: extract_invoice_fields(REAL_PDF_TEXT)):
        return watcher.process_once()


# --- detection ------------------------------------------------------------

def test_watcher_uses_the_specified_query_and_client_user_id(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    assert watcher.user_id == "telegram_999653395"
    query = dict(recorder.calls)["GMAIL_FETCH_EMAILS"]["query"]
    assert query == 'subject:"[XBLASTE]" has:attachment filename:pdf'


def test_a_matching_email_produces_a_pending_invoice(watcher, db_path):
    pendings = _run_cycle(watcher, Recorder())
    assert len(pendings) == 1
    p = pendings[0]
    assert p.fields.numero == "FAC-TEST-2026-001"
    assert p.scope == "purchases"          # X BLASTE est le client -> achat
    row = get_gmail_message(db_path, p.message_id)
    assert row["status"] == "pending"


def test_nothing_is_written_to_sheets_or_drive_before_confirmation(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    forbidden = [s for s in recorder.slugs if "UPSERT" in s or "DRIVE" in s or "UPDATE" in s]
    assert forbidden == [], f"ecriture prematuree detectee: {forbidden}"


def test_gmail_is_only_read_never_written(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    gmail = [s for s in recorder.slugs if s.startswith("GMAIL_")]
    assert gmail and all(
        not any(k in s for k in ("SEND", "DELETE", "TRASH", "DRAFT", "MODIFY", "REPLY"))
        for s in gmail
    )


# --- anti-doublon ---------------------------------------------------------

def test_the_same_email_is_never_processed_twice(watcher, db_path):
    first = _run_cycle(watcher, Recorder())
    second = _run_cycle(watcher, Recorder())
    assert len(first) == 1
    assert second == [], "le meme message_id a ete traite une seconde fois"


def test_dedup_survives_a_restart(db_path):
    w1 = GmailWatcher(api_key="k", chat_id=999653395, db_path=db_path, spreadsheet_id="s")
    assert len(_run_cycle(w1, Recorder())) == 1
    # Nouveau watcher = redemarrage du conteneur : la base doit suffire.
    w2 = GmailWatcher(api_key="k", chat_id=999653395, db_path=db_path, spreadsheet_id="s")
    assert _run_cycle(w2, Recorder()) == []


def test_an_email_without_pdf_is_marked_skipped_not_retried_forever(watcher, db_path):
    class NoPdf(Recorder):
        def __call__(self, slug, arguments):
            if slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID":
                self.calls.append((slug, arguments))
                return {**MESSAGE, "attachmentList": []}
            return super().__call__(slug, arguments)

    with patch.object(GmailWatcher, "_execute", side_effect=NoPdf()):
        assert watcher.process_once() == []
    assert get_gmail_message(db_path, MESSAGE["messageId"])["status"] == "skipped"


# --- isolation entre clients ---------------------------------------------

def test_two_chats_use_two_different_composio_user_ids(db_path):
    a = GmailWatcher(api_key="k", chat_id=111111, db_path=db_path)
    b = GmailWatcher(api_key="k", chat_id=222222, db_path=db_path)
    assert a.user_id == "telegram_111111"
    assert b.user_id == "telegram_222222"
    assert a.user_id != b.user_id


# --- apercu ---------------------------------------------------------------

def test_preview_shows_every_field_and_says_nothing_is_written(watcher):
    pending = _run_cycle(watcher, Recorder())[0]
    preview = build_preview(pending)
    for expected in ("FAC-TEST-2026-001", "2026-08-21", "2026-09-20",
                     "ATLAS BUREAU SARL", "X BLASTE", "4 000,00", "800,00",
                     "4 800,00", "NON PAYEE", "Virement bancaire"):
        assert expected in preview, f"absent de l'apercu : {expected}"
    assert "Rien n'a encore ete ecrit" in preview


def test_preview_lists_missing_fields_when_extraction_is_partial(watcher):
    from app.invoice_pdf import extract_invoice_fields

    with patch.object(GmailWatcher, "_execute", side_effect=Recorder()), \
         patch.object(GmailWatcher, "download_attachment", return_value=b"x"), \
         patch("app.gmail_watcher.extract_from_pdf_bytes",
               return_value=extract_invoice_fields("FACTURE\nDivers\nfin")):
        pending = watcher.process_once()[0]
    preview = build_preview(pending)
    assert "Champs introuvables" in preview
    assert "non devines" in preview


# --- confirmation / refus -------------------------------------------------

def test_confirm_writes_to_the_purchases_tab(watcher, db_path):
    pending = _run_cycle(watcher, Recorder())[0]
    recorder = Recorder()
    with patch.object(GmailWatcher, "_execute", side_effect=recorder):
        result = watcher.confirm(pending.message_id)

    upserts = [a for s, a in recorder.calls if s == "GOOGLESHEETS_UPSERT_ROWS"]
    assert len(upserts) == 1
    assert upserts[0]["sheetName"] == "05_FACTURES_ACHATS"
    row = upserts[0]["rows"][0]
    assert row[0] == "FAC-TEST-2026-001"          # ID = numero (idempotent)
    assert "4000.00 MAD" in row[6]                 # HT
    assert "4800.00 MAD" in row[9]                 # TTC
    assert "05_FACTURES_ACHATS" in result
    assert get_gmail_message(db_path, pending.message_id)["status"] == "confirmed"


def test_confirm_is_idempotent_and_never_writes_twice(watcher, db_path):
    pending = _run_cycle(watcher, Recorder())[0]
    with patch.object(GmailWatcher, "_execute", side_effect=Recorder()):
        watcher.confirm(pending.message_id)
    second = Recorder()
    with patch.object(GmailWatcher, "_execute", side_effect=second):
        message = watcher.confirm(pending.message_id)
    assert "deja ete enregistree" in message
    assert [s for s in second.slugs if "UPSERT" in s] == []


def test_refuse_writes_nothing_at_all(watcher, db_path):
    pending = _run_cycle(watcher, Recorder())[0]
    recorder = Recorder()
    with patch.object(GmailWatcher, "_execute", side_effect=recorder):
        message = watcher.refuse(pending.message_id)
    assert recorder.calls == []
    assert "Rien n'a ete ecrit" in message
    assert get_gmail_message(db_path, pending.message_id)["status"] == "refused"


def test_a_refused_invoice_cannot_be_confirmed_afterwards(watcher):
    pending = _run_cycle(watcher, Recorder())[0]
    watcher.refuse(pending.message_id)
    recorder = Recorder()
    with patch.object(GmailWatcher, "_execute", side_effect=recorder):
        message = watcher.confirm(pending.message_id)
    assert "refusee" in message
    assert [s for s in recorder.slugs if "UPSERT" in s] == []


def test_confirm_on_unknown_message_raises_clearly(watcher):
    with pytest.raises(GmailWatcherError):
        watcher.confirm("message-inexistant")


# --- classement ventes / achats ------------------------------------------

def test_invoice_addressed_to_our_company_is_a_purchase(watcher):
    from app.invoice_pdf import extract_invoice_fields

    assert watcher.decide_scope(extract_invoice_fields(REAL_PDF_TEXT)) == "purchases"


def test_invoice_issued_by_our_company_is_a_sale(watcher):
    from app.invoice_pdf import extract_invoice_fields

    text = REAL_PDF_TEXT.replace("EMETTEUR\nATLAS BUREAU SARL", "EMETTEUR\nX BLASTE") \
                        .replace("CLIENT\nX BLASTE", "CLIENT\nATLAS BUREAU SARL")
    assert watcher.decide_scope(extract_invoice_fields(text)) == "sales"


# --- securite -------------------------------------------------------------

def test_callback_data_fits_telegram_64_byte_limit():
    for prefix in (CALLBACK_CONFIRM_PREFIX, CALLBACK_REFUSE_PREFIX):
        assert len((prefix + MESSAGE["messageId"]).encode()) <= 64


def test_errors_never_leak_the_api_key(db_path):
    w = GmailWatcher(api_key="super-secret", chat_id=1, db_path=db_path)
    with patch("httpx.Client", side_effect=RuntimeError("boom")):
        with pytest.raises(GmailWatcherError) as exc:
            w.search_messages()
    assert "super-secret" not in str(exc.value)


def test_watcher_is_disabled_without_a_configured_chat(db_path):
    assert GmailWatcher(api_key="k", chat_id=0, db_path=db_path).is_configured is False
    assert GmailWatcher(api_key="", chat_id=42, db_path=db_path).is_configured is False
    assert GmailWatcher(api_key="k", chat_id=42, db_path=db_path).is_configured is True
