"""Tests du worker Gmail.

Ce que ces tests garantissent :

  - une facture lisible, complete, coherente et non ambigue est importee
    AUTOMATIQUEMENT, sans confirmation, avec une notification "Facture
    importée avec succès" ;
  - une facture douteuse (montant illisible, HT+TVA != TTC, ICE manquant,
    fournisseur ambigu, plusieurs valeurs possibles, avoir, doublon
    incertain) demande une validation humaine et n'ecrit RIEN ;
  - un doublon certain (meme ICE fournisseur + meme numero) n'est jamais
    ecrit : le client est seulement informe ;
  - les valeurs ecrites respectent les conventions reelles du classeur
    (ID stable FA-..., ID fournisseur, nombres natifs, taux 20, statut
    Impayee, formules recopiees) ;
  - Gmail reste en lecture seule et aucune cle API ne fuit.

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

TABS = ["00_DASHBOARD", "03_FOURNISSEURS", "04_FACTURES_VENTES",
        "05_FACTURES_ACHATS", "14_IMPORTS_LOG"]

# Les 5 fournisseurs de demonstration : ATLAS BUREAU SARL n'y figure pas,
# il devra donc etre cree a partir de son ICE.
DEMO_SUPPLIERS = [
    ["FRS-001", "Fournitures Atlas SARL (DEMO)", "DEMO-ICE-200341"],
    ["FRS-002", "Papeterie Zellige (DEMO)", "DEMO-ICE-200342"],
    ["FRS-003", "Transport Sindibad (DEMO)", "DEMO-ICE-200343"],
    ["FRS-004", "Cyber Cafe Medina Services (DEMO)", "DEMO-ICE-200344"],
    ["FRS-005", "Imprimerie Argan (DEMO)", "DEMO-ICE-200345"],
]

# 12 lignes d'achat existantes (A, B, C, D) : la prochaine sera la 14e ligne.
DEMO_PURCHASES = [
    [f"FA-2026-{i:03d}", 46200 + i, f"FAC-ACH-2026-{i:03d}", f"FRS-{(i % 5) + 1:03d}"]
    for i in range(1, 13)
]


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
    """Faux classeur + faux Gmail. Enregistre chaque outil appele, pour
    prouver ce qui a - ou surtout n'a PAS - ete execute."""

    def __init__(self, suppliers=None, purchases=None):
        self.calls: list[tuple[str, dict]] = []
        self.suppliers = [list(r) for r in (DEMO_SUPPLIERS if suppliers is None else suppliers)]
        self.purchases = [list(r) for r in (DEMO_PURCHASES if purchases is None else purchases)]

    @property
    def slugs(self) -> list[str]:
        return [s for s, _ in self.calls]

    def writes(self) -> list[dict]:
        return [a for s, a in self.calls if s == "GOOGLESHEETS_VALUES_UPDATE"]

    def write_to(self, prefix: str) -> list[dict]:
        return [a for a in self.writes() if a["range"].startswith(prefix)]

    def _batch_get(self, arguments):
        a1 = (arguments.get("ranges") or [""])[0]
        if a1.startswith("03_FOURNISSEURS"):
            return {"valueRanges": [{"values": self.suppliers}]}
        if a1.startswith("05_FACTURES_ACHATS!A2:A"):
            return {"valueRanges": [{"values": [[r[0]] for r in self.purchases]}]}
        if a1.startswith("05_FACTURES_ACHATS"):
            return {"valueRanges": [{"values": self.purchases}]}
        return {"valueRanges": [{"values": []}]}

    def __call__(self, slug, arguments):
        self.calls.append((slug, arguments))
        if slug == "GMAIL_FETCH_EMAILS":
            return {"messages": [{"messageId": MESSAGE["messageId"]}]}
        if slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID":
            return MESSAGE
        if slug == "GMAIL_GET_ATTACHMENT":
            return {"file": {"s3url": "https://example.invalid/f.pdf",
                             "mimetype": "application/pdf"}}
        if slug == "GOOGLESHEETS_BATCH_GET":
            return self._batch_get(arguments)
        if slug == "GOOGLESHEETS_GET_SPREADSHEET_INFO":
            if arguments.get("ranges"):
                return {"sheets": [{"data": [{"rowData": [{"values": [
                    {"effectiveFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}}
                ]}]}]}]}
            return {"sheets": [
                {"properties": {"title": t, "sheetId": i}} for i, t in enumerate(TABS)
            ]}
        if slug == "GOOGLEDRIVE_UPLOAD_FROM_URL":
            return {"id": "drive-file-id"}
        return {}


def _run_cycle(watcher, recorder, text=REAL_PDF_TEXT):
    from app.invoice_pdf import extract_invoice_fields

    with patch.object(GmailWatcher, "_execute", side_effect=recorder), \
         patch.object(GmailWatcher, "download_attachment", return_value=b"%PDF-fake"), \
         patch("app.gmail_watcher.extract_from_pdf_bytes",
               side_effect=lambda b: extract_invoice_fields(text)):
        return watcher.process_once()


# --- detection ------------------------------------------------------------

def test_watcher_uses_the_specified_query_and_client_user_id(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    assert watcher.user_id == "telegram_999653395"
    query = dict(recorder.calls)["GMAIL_FETCH_EMAILS"]["query"]
    assert query.startswith("in:inbox has:attachment filename:pdf")
    assert " after:" in query, "le curseur doit borner la requete"


def test_gmail_is_only_read_never_written(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    gmail = [s for s in recorder.slugs if s.startswith("GMAIL_")]
    assert gmail and all(
        not any(k in s for k in ("SEND", "DELETE", "TRASH", "DRAFT", "MODIFY", "REPLY"))
        for s in gmail
    )


# --- import automatique ---------------------------------------------------

def test_a_clean_invoice_is_imported_without_any_confirmation(watcher, db_path):
    outcomes = _run_cycle(watcher, Recorder())
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.decision.action == "auto"
    assert outcome.needs_buttons is False
    assert "Facture importée avec succès" in outcome.message
    assert get_gmail_message(db_path, MESSAGE["messageId"])["status"] == "confirmed"


def test_the_success_notification_carries_every_requested_detail(watcher):
    message = _run_cycle(watcher, Recorder())[0].message
    for expected in ("FAC-TEST-2026-001", "ATLAS BUREAU SARL", "4000.00",
                     "800.00", "4800.00", "05_FACTURES_ACHATS", "14"):
        assert expected in message, f"absent de la notification : {expected}"


def test_the_written_row_follows_the_workbook_conventions(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    main = recorder.write_to("05_FACTURES_ACHATS!A14:J14")
    assert len(main) == 1
    row = main[0]["values"][0]
    assert main[0]["value_input_option"] == "RAW"
    assert row[0] == "FA-2026-013"          # ID stable, PAS le numero de facture
    assert row[2] == "FAC-TEST-2026-001"    # numero de facture en colonne C
    assert row[3] == "FRS-006"              # ID fournisseur en colonne D
    assert row[1] == 46255                  # date en numero de serie (2026-08-21)
    assert row[6] == 4000.0                 # HT : nombre natif, jamais "4000.00 MAD"
    assert row[7] == 20.0                   # taux TVA au format du classeur (20, pas 0,2)
    assert row[8] == 800.0
    assert row[9] == 4800.0
    for cell in row[6:10]:
        assert not isinstance(cell, str), f"montant ecrit en chaine : {cell!r}"


def test_status_uses_the_workbook_vocabulary(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    tail = recorder.write_to("05_FACTURES_ACHATS!N14:P14")[0]["values"][0]
    assert tail[0] == 46285          # echeance 2026-09-20 en numero de serie
    assert tail[1] == 0.0            # montant paye
    assert tail[2] == "Impayee"      # et surtout pas "NON PAYEE"


def test_formulas_are_copied_into_k_l_m_and_q(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    klm = recorder.write_to("05_FACTURES_ACHATS!K14:M14")[0]
    q = recorder.write_to("05_FACTURES_ACHATS!Q14")[0]
    assert klm["value_input_option"] == "USER_ENTERED"
    assert klm["values"][0][0] == "=ROUND(G14+I14;2)"
    assert klm["values"][0][1] == "=J14-K14"
    assert klm["values"][0][2] == '=IF(COUNTIF($C$2:$C$14;C14)>1;"DOUBLON";"")'
    assert q["values"][0][0] == "=IF(AND(O14<J14;TODAY()>N14);TODAY()-N14;0)"


def test_number_formats_and_status_validation_are_applied(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    formats = {a["range"]: a for s, a in recorder.calls if s == "GOOGLESHEETS_FORMAT_CELL"}
    assert formats["G14"]["number_format_pattern"] == '#,##0.00 "MAD"'
    assert formats["H14"]["number_format_pattern"] == '0"%"'
    assert formats["B14"]["number_format_pattern"] == "yyyy-mm-dd"
    validations = [a for s, a in recorder.calls if s == "GOOGLESHEETS_SET_DATA_VALIDATION_RULE"]
    assert len(validations) == 1
    assert validations[0]["values"] == ["Payee", "Partiellement payee", "Impayee"]
    assert validations[0]["start_column_index"] == 15   # colonne P


def test_no_other_invoice_row_is_ever_touched(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    for write in recorder.write_to("05_FACTURES_ACHATS"):
        assert "14" in write["range"], f"ecriture hors de la ligne 14 : {write['range']}"


# --- fournisseur ----------------------------------------------------------

def test_an_unknown_supplier_is_created_from_its_ice(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    created = recorder.write_to("03_FOURNISSEURS")
    assert len(created) == 1
    row = created[0]["values"][0]
    assert row[0] == "FRS-006"
    assert row[1] == "ATLAS BUREAU SARL"
    assert row[2] == "002345678000043"       # ICE du fournisseur, pas celui du client


def test_a_known_supplier_is_reused_and_never_duplicated(watcher):
    recorder = Recorder(
        suppliers=DEMO_SUPPLIERS + [["FRS-006", "ATLAS BUREAU SARL", "002345678000043"]]
    )
    _run_cycle(watcher, recorder)
    assert recorder.write_to("03_FOURNISSEURS") == []
    row = recorder.write_to("05_FACTURES_ACHATS!A14:J14")[0]["values"][0]
    assert row[3] == "FRS-006"


def test_a_supplier_with_the_same_name_but_another_ice_is_ambiguous(watcher):
    recorder = Recorder(
        suppliers=DEMO_SUPPLIERS + [["FRS-006", "ATLAS BUREAU SARL", "999999999999999"]]
    )
    outcome = _run_cycle(watcher, recorder)[0]
    assert outcome.decision.action == "review"
    assert any("ambigu" in r for r in outcome.decision.reasons)
    assert recorder.write_to("05_FACTURES_ACHATS") == []


# --- lignes de detail et journal ------------------------------------------

def test_invoice_detail_lines_go_to_a_dedicated_tab(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    added = [a for s, a in recorder.calls if s == "GOOGLESHEETS_ADD_SHEET"]
    assert added and added[0]["title"] == "16_LIGNES_FACTURES"
    lines = recorder.write_to("16_LIGNES_FACTURES!A2")
    assert len(lines) == 1
    rows = lines[0]["values"]
    assert len(rows) == 2
    assert rows[0][0] == "FA-2026-013"        # lie a l'ID de facture
    assert rows[0][4] == "Ramettes papier A4 premium"
    assert rows[0][5] == 2.0 and rows[0][8] == 1500.0
    assert rows[1][8] == 2500.0


def test_the_import_log_keeps_gmail_and_drive_information(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    log = recorder.write_to("14_IMPORTS_LOG")
    assert len(log) == 1
    row = log[0]["values"][0]
    assert row[2] == "FA-2026-013"
    assert row[3] == "Créé"
    detail = row[5]
    for expected in ("FAC-TEST-2026-001", "002345678000043", "05_FACTURES_ACHATS ligne 14",
                     MESSAGE["messageId"], "Facture_test_X_BLASTE_FAC-TEST-2026-001.pdf",
                     "drive.google.com"):
        assert expected in detail, f"absent du journal d'import : {expected}"


# --- cas exigeant une validation humaine ----------------------------------

def _review(watcher, recorder, text):
    outcome = _run_cycle(watcher, recorder, text=text)[0]
    assert outcome.decision.action == "review"
    assert outcome.needs_buttons is True
    assert recorder.write_to("05_FACTURES_ACHATS") == []
    return outcome


def test_an_incoherent_total_requires_human_validation(watcher):
    text = REAL_PDF_TEXT.replace(" 4 800.00 MAD", " 4 900.00 MAD")
    outcome = _review(watcher, Recorder(), text)
    assert any("HT + TVA ne correspond pas au TTC" in r for r in outcome.decision.reasons)


def test_a_missing_supplier_ice_requires_human_validation(watcher):
    text = REAL_PDF_TEXT.replace("ICE: 002345678000043   IF: 18765432", "IF: 18765432")
    outcome = _review(watcher, Recorder(), text)
    assert any("ICE du fournisseur absent" in r for r in outcome.decision.reasons)


def test_an_unreadable_amount_requires_human_validation(watcher):
    text = REAL_PDF_TEXT.replace(" 4 000.00 MAD", " illisible")
    outcome = _review(watcher, Recorder(), text)
    assert any("montant HT" in r for r in outcome.decision.reasons)


def test_a_credit_note_requires_human_validation(watcher):
    text = REAL_PDF_TEXT.replace("FACTURE\n", "FACTURE D AVOIR\n")
    outcome = _review(watcher, Recorder(), text)
    assert any("avoir" in r for r in outcome.decision.reasons)


def test_several_possible_values_require_human_validation(watcher):
    text = REAL_PDF_TEXT.replace(
        " TOTAL TTC\n 4 800.00 MAD", " TOTAL TTC\n 4 800.00 MAD\n TOTAL TTC\n 5 000.00 MAD"
    )
    outcome = _review(watcher, Recorder(), text)
    assert any("plusieurs valeurs possibles" in r for r in outcome.decision.reasons)


def test_review_message_shows_the_reasons_and_states_nothing_was_written(watcher):
    text = REAL_PDF_TEXT.replace("ICE: 002345678000043   IF: 18765432", "IF: 18765432")
    outcome = _review(watcher, Recorder(), text)
    assert "Validation humaine demandee car :" in outcome.message
    assert "Rien n'a encore ete ecrit" in outcome.message


# --- doublons -------------------------------------------------------------

def test_a_certain_duplicate_is_never_written_only_reported(watcher, db_path):
    _run_cycle(watcher, Recorder())          # premier import : ecrit la ligne 14
    assert get_gmail_message(db_path, MESSAGE["messageId"])["status"] == "confirmed"

    # Le meme email revient sous un autre message_id (renvoi / transfert).
    recorder = Recorder(
        purchases=DEMO_PURCHASES + [["FA-2026-013", 46255, "FAC-TEST-2026-001", "FRS-006"]],
        suppliers=DEMO_SUPPLIERS + [["FRS-006", "ATLAS BUREAU SARL", "002345678000043"]],
    )
    with patch.dict(MESSAGE, {"messageId": "second-message-id"}):
        outcome = _run_cycle(watcher, recorder)[0]
    assert outcome.decision.action == "duplicate"
    assert outcome.needs_buttons is False
    assert "deja importee" in outcome.message
    assert recorder.write_to("05_FACTURES_ACHATS") == []


def test_the_duplicate_key_is_ice_plus_invoice_number():
    from app.invoice_policy import fingerprint

    assert fingerprint("002345678000043", "FAC-TEST-2026-001") == \
        "002345678000043|FAC-TEST-2026-001"
    # Meme numero, fournisseur different -> ce n'est PAS le meme doublon.
    assert fingerprint("111", "F-1") != fingerprint("222", "F-1")
    # Sans ICE, aucun doublon ne peut etre affirme.
    assert fingerprint(None, "F-1") == ""
    assert fingerprint("111", None) == ""


def test_the_same_number_from_another_supplier_is_only_uncertain(watcher):
    recorder = Recorder(
        purchases=DEMO_PURCHASES + [["FA-2026-013", 46255, "FAC-TEST-2026-001", "FRS-002"]],
    )
    outcome = _run_cycle(watcher, recorder)[0]
    assert outcome.decision.action == "review"
    assert any("doublon possible" in r for r in outcome.decision.reasons)
    assert recorder.write_to("05_FACTURES_ACHATS") == []


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


# --- apercu (cas douteux uniquement) --------------------------------------

def test_preview_shows_every_field_including_the_ice(watcher):
    text = REAL_PDF_TEXT.replace(" 4 800.00 MAD", " 4 900.00 MAD")
    outcome = _run_cycle(watcher, Recorder(), text=text)[0]
    preview = build_preview(outcome.pending)
    for expected in ("FAC-TEST-2026-001", "2026-08-21", "2026-09-20",
                     "ATLAS BUREAU SARL", "X BLASTE", "002345678000043",
                     "4 000,00", "800,00", "NON PAYEE", "Virement bancaire"):
        assert expected in preview, f"absent de l'apercu : {expected}"
    assert "Rien n'a encore ete ecrit" in preview


PARTIAL_INVOICE = "FACTURE\nDATE DE FACTURE\n21/08/2026\n TOTAL TTC\n 1 200.00 MAD\n"


def test_preview_lists_missing_fields_when_extraction_is_partial(watcher):
    outcome = _run_cycle(watcher, Recorder(), text=PARTIAL_INVOICE)[0]
    preview = build_preview(outcome.pending)
    assert "Champs introuvables" in preview
    assert "non devines" in preview


# --- confirmation / refus (chemin humain) ---------------------------------

def test_confirm_after_review_writes_the_row(watcher, db_path):
    text = REAL_PDF_TEXT.replace(" 4 800.00 MAD", " 4 900.00 MAD")
    outcome = _run_cycle(watcher, Recorder(), text=text)[0]
    recorder = Recorder()
    with patch.object(GmailWatcher, "_execute", side_effect=recorder):
        result = watcher.confirm(outcome.pending.message_id)
    assert "Facture importée avec succès" in result
    assert recorder.write_to("05_FACTURES_ACHATS!A14:J14")
    assert get_gmail_message(db_path, outcome.pending.message_id)["status"] == "confirmed"


def test_confirm_is_idempotent_and_never_writes_twice(watcher, db_path):
    text = REAL_PDF_TEXT.replace(" 4 800.00 MAD", " 4 900.00 MAD")
    outcome = _run_cycle(watcher, Recorder(), text=text)[0]
    with patch.object(GmailWatcher, "_execute", side_effect=Recorder()):
        watcher.confirm(outcome.pending.message_id)
    second = Recorder()
    with patch.object(GmailWatcher, "_execute", side_effect=second):
        message = watcher.confirm(outcome.pending.message_id)
    assert "deja ete enregistree" in message
    assert second.write_to("05_FACTURES_ACHATS") == []


def test_refuse_writes_nothing_at_all(watcher, db_path):
    text = REAL_PDF_TEXT.replace(" 4 800.00 MAD", " 4 900.00 MAD")
    outcome = _run_cycle(watcher, Recorder(), text=text)[0]
    recorder = Recorder()
    with patch.object(GmailWatcher, "_execute", side_effect=recorder):
        message = watcher.refuse(outcome.pending.message_id)
    assert recorder.calls == []
    assert "Rien n'a ete ecrit" in message
    assert get_gmail_message(db_path, outcome.pending.message_id)["status"] == "refused"


def test_a_refused_invoice_cannot_be_confirmed_afterwards(watcher):
    text = REAL_PDF_TEXT.replace(" 4 800.00 MAD", " 4 900.00 MAD")
    outcome = _run_cycle(watcher, Recorder(), text=text)[0]
    watcher.refuse(outcome.pending.message_id)
    recorder = Recorder()
    with patch.object(GmailWatcher, "_execute", side_effect=recorder):
        message = watcher.confirm(outcome.pending.message_id)
    assert "refusee" in message
    assert recorder.write_to("05_FACTURES_ACHATS") == []


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


# --- curseur Gmail durable ------------------------------------------------
# La requete couvrant desormais toute la boite de reception, le curseur est le
# seul garde-fou contre l'import de l'historique.

def test_the_query_is_bounded_by_a_cursor(watcher):
    recorder = Recorder()
    _run_cycle(watcher, recorder)
    query = dict(recorder.calls)["GMAIL_FETCH_EMAILS"]["query"]
    assert query.startswith("in:inbox has:attachment filename:pdf after:")
    epoch = int(query.rsplit("after:", 1)[1])
    assert epoch > 1_600_000_000, "le curseur doit etre un epoch plausible"


def test_the_cursor_is_frozen_at_first_start_and_survives_a_restart(db_path):
    w1 = GmailWatcher(api_key="k", chat_id=999653395, db_path=db_path, spreadsheet_id="s")
    first = w1.cursor_epoch()
    # Nouveau watcher = redemarrage du conteneur.
    w2 = GmailWatcher(api_key="k", chat_id=999653395, db_path=db_path, spreadsheet_id="s")
    assert w2.cursor_epoch() == first
    assert w2.effective_query().endswith(f"after:{first}")


def test_each_client_has_its_own_cursor(db_path):
    a = GmailWatcher(api_key="k", chat_id=111111, db_path=db_path)
    b = GmailWatcher(api_key="k", chat_id=222222, db_path=db_path)
    a.cursor_epoch()
    b.cursor_epoch()
    from app.db import connect

    with connect(db_path) as conn:
        chats = {r[0] for r in conn.execute("SELECT chat_id FROM gmail_cursor")}
    assert chats == {"111111", "222222"}


# --- reconnaissance de facture par le contenu -----------------------------

QUOTE_PDF = (
    "DEVIS\nN° DEV-2026-010\nDATE DE FACTURE\n21/08/2026\n"
    " Total HT\n 1 000.00 MAD\n TOTAL TTC\n 1 200.00 MAD\n"
)


def test_a_pdf_that_is_not_an_invoice_is_ignored_without_writing_anything(watcher, db_path):
    recorder = Recorder()
    outcomes = _run_cycle(watcher, recorder, text=QUOTE_PDF)
    assert outcomes == []
    assert recorder.writes() == []
    assert get_gmail_message(db_path, MESSAGE["messageId"])["status"] == "skipped"


def test_a_non_invoice_pdf_is_not_retried_at_the_next_cycle(watcher):
    _run_cycle(watcher, Recorder(), text=QUOTE_PDF)
    second = Recorder()
    assert _run_cycle(watcher, second, text=QUOTE_PDF) == []
    assert second.writes() == []


# --- reprise idempotente --------------------------------------------------

class DriveDown(Recorder):
    """Sheets fonctionne, Drive est indisponible."""

    def __call__(self, slug, arguments):
        if slug.startswith("GOOGLEDRIVE_"):
            self.calls.append((slug, arguments))
            raise GmailWatcherError("Drive indisponible")
        return super().__call__(slug, arguments)


def test_a_drive_failure_leaves_the_accounting_row_written_once(watcher, db_path):
    recorder = DriveDown()
    outcomes = _run_cycle(watcher, recorder)
    # La ligne comptable est bien ecrite, une seule fois.
    assert len(recorder.write_to("05_FACTURES_ACHATS!A14:J14")) == 1
    # L'import n'est pas declare termine.
    assert get_gmail_message(db_path, MESSAGE["messageId"])["status"] == "partial"
    # Le client est informe que la comptabilite est juste, sans bouton a
    # cliquer : il n'y a rien a decider, seulement un archivage a terminer.
    assert len(outcomes) == 1
    assert outcomes[0].needs_buttons is False
    assert "enregistree dans le classeur" in outcomes[0].message
    assert "archivage Drive incomplet" in outcomes[0].message
    # Et surtout : pas de journal d'import tant que le lien Drive manque.
    assert recorder.write_to("14_IMPORTS_LOG") == []


def test_the_next_cycle_finishes_the_archiving_without_a_second_row(watcher, db_path):
    _run_cycle(watcher, DriveDown())
    second = Recorder()
    outcomes = _run_cycle(watcher, second)

    # AUCUNE nouvelle ecriture dans l'onglet des factures.
    assert second.write_to("05_FACTURES_ACHATS") == [], "une deuxieme ligne a ete ecrite"
    # Mais l'archivage et le journal sont bien termines.
    assert [s for s in second.slugs if s == "GOOGLEDRIVE_UPLOAD_FROM_URL"]
    log = second.write_to("14_IMPORTS_LOG")
    assert len(log) == 1
    assert "drive.google.com" in log[0]["values"][0][5]
    assert get_gmail_message(db_path, MESSAGE["messageId"])["status"] == "confirmed"
    assert len(outcomes) == 1 and outcomes[0].decision.action == "auto"


def test_a_third_cycle_writes_nothing_at_all(watcher):
    _run_cycle(watcher, DriveDown())
    _run_cycle(watcher, Recorder())
    third = Recorder()
    outcomes = _run_cycle(watcher, third)
    assert third.writes() == [], "polling suivant : plus aucune ecriture"
    assert outcomes == []


def test_the_detail_lines_are_never_written_twice_on_resume(watcher):
    _run_cycle(watcher, DriveDown())
    second = Recorder()
    _run_cycle(watcher, second)
    assert second.write_to("16_LIGNES_FACTURES") == []
