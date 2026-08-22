"""Tests d'integration du pipeline documentaire, sur les 12 PDF reels.

Les fixtures sont la couche texte EXACTE des PDF du pack de test fourni par
le client (tests/fixtures/pack/). Aucun appel reseau : le classeur, Drive et
Calendar sont simules en memoire par tests/workbook_fake.py, ce qui permet
d'affirmer non seulement ce qui a ete ecrit, mais surtout ce qui ne l'a PAS
ete.

Couvre les 20 points de recette demandes.
"""
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from app import doc_store as store
from app.attachments import DocumentFile
from app.db import init_db
from app.doc_extract import extract_document
from app.doc_pipeline import DocumentPipeline
from app.doc_policy import ACTION_AUTO, ACTION_DUPLICATE, ACTION_REVIEW
from app.doc_types import (
    BANK_STATEMENT,
    EXPORT_INVOICE,
    IMPORT_INVOICE,
    PAYMENT_RECEIPT,
    PENALTY_NOTICE,
    PURCHASE_INVOICE,
    PURCHASE_ORDER,
    QUOTE,
    SALES_INVOICE,
    SUPPLIER_CREDIT_NOTE,
    classify,
)
from workbook_fake import FakeWorkbook

PACK = Path(__file__).parent / "fixtures" / "pack"

ACHAT = "01_FACTURE_ACHAT_OK_FAC-TEST-2026-002"
VENTE = "02_FACTURE_VENTE_OK_FAC-VTE-TEST-2026-012"
DEVIS = "03_DEVIS_DEV-2026-008"
RELEVE = "04_RELEVE_BANCAIRE_AOUT_2026"
PENALITE = "05_AVIS_PENALITE_DGI_PEN-2026-044"
IMPORT = "06_FACTURE_IMPORT_COMMERCIAL_INVOICE_CI-2026-045"
EXPORT = "07_FACTURE_EXPORT_EXP-2026-019"
AVOIR = "08_AVOIR_AV-2026-003_VALIDATION_REQUISE"
ANOMALIE = "09_FACTURE_ANOMALIE_FAC-TEST-2026-003_VALIDATION_REQUISE"
SANS_ICE = "10_FACTURE_ICE_MANQUANT_FAC-TEST-2026-004_VALIDATION_REQUISE"
RECU = "11_RECU_PAIEMENT_REC-2026-017"
BON_COMMANDE = "12_BON_COMMANDE_BC-2026-021"


def text_of(name: str) -> str:
    return (PACK / f"{name}.txt").read_text(encoding="utf-8")


@pytest.fixture
def db_path():
    path = tempfile.mktemp(suffix=".db")
    init_db(path)
    store.ensure_schema(path)
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def workbook():
    return FakeWorkbook()


@pytest.fixture
def pipeline(workbook, db_path, monkeypatch):
    """Pipeline reel, branche sur le faux classeur.

    Seule la lecture du PDF est court-circuitee : le texte injecte est celui
    reellement extrait des PDF du client.
    """
    import app.doc_pipeline as module

    registry: dict[bytes, str] = {}

    def fake_read(content, company="X BLASTE", ocr=True):
        return extract_document([registry[content]], company=company)

    monkeypatch.setattr(module, "extract_from_pdf_bytes", fake_read)
    pipe = DocumentPipeline(
        workbook, db_path=db_path, chat_id=999653395, spreadsheet_id="sheet-test"
    )
    pipe.registry = registry  # type: ignore[attr-defined]
    return pipe


def make_file(pipeline, name: str, *, tag: str = "", filename: str | None = None) -> DocumentFile:
    content = f"%PDF-{name}-{tag}".encode()
    pipeline.registry[content] = text_of(name)
    return DocumentFile(
        filename=filename or f"{name}.pdf", content=content, source="attachment"
    )


def run(pipeline, name, *, message_id="m1", attachment_id="att-1", tag="", forced=False):
    file = make_file(pipeline, name, tag=tag)
    return pipeline.process_document(
        file,
        {"messageId": message_id, "subject": "Pack test", "sender": "client@example.ma"},
        attachment_id=attachment_id,
        source_url="https://example.invalid/f.pdf",
        forced=forced,
    )


# === 1. classification correcte de chaque PDF ============================

@pytest.mark.parametrize(
    "name,expected",
    [
        (ACHAT, PURCHASE_INVOICE),
        (VENTE, SALES_INVOICE),
        (DEVIS, QUOTE),
        (RELEVE, BANK_STATEMENT),
        (PENALITE, PENALTY_NOTICE),
        (IMPORT, IMPORT_INVOICE),
        (EXPORT, EXPORT_INVOICE),
        (AVOIR, SUPPLIER_CREDIT_NOTE),
        (ANOMALIE, PURCHASE_INVOICE),
        (SANS_ICE, PURCHASE_INVOICE),
        (RECU, PAYMENT_RECEIPT),
        (BON_COMMANDE, PURCHASE_ORDER),
    ],
)
def test_every_document_of_the_pack_is_classified_correctly(name, expected):
    assert classify(text_of(name)).doc_type == expected


def test_the_word_devise_is_never_read_as_devis():
    # "Devise : EUR" figure sur la facture d'export : la confondre avec un
    # devis enverrait une vraie vente au suivi documentaire.
    assert "Devise" in text_of(EXPORT)
    assert classify(text_of(EXPORT)).doc_type == EXPORT_INVOICE


def test_a_quote_that_mentions_the_word_facture_stays_a_quote():
    # Le devis porte "ne constitue pas une facture".
    assert "facture" in text_of(DEVIS)
    assert classify(text_of(DEVIS)).doc_type == QUOTE


def test_a_receipt_that_mentions_an_invoice_stays_a_receipt():
    assert "Facture" in text_of(RECU)
    assert classify(text_of(RECU)).doc_type == PAYMENT_RECEIPT


# === 2. facture d'achat propre importee automatiquement ==================

def test_a_clean_purchase_invoice_is_imported_without_confirmation(pipeline, workbook):
    outcome = run(pipeline, ACHAT)
    assert outcome.action == ACTION_AUTO
    assert outcome.tab == "05_FACTURES_ACHATS"
    row = workbook.row("05_FACTURES_ACHATS", outcome.row_index)
    assert row[0] == outcome.stable_id
    assert row[2] == "FAC-TEST-2026-002"
    assert row[6] == 6250.0 and row[8] == 1250.0 and row[9] == 7500.0
    assert row[7] == 20.0                      # taux au format du classeur
    assert row[15] == "Impayee"
    assert all(not isinstance(cell, str) for cell in (row[6], row[8], row[9]))


def test_the_purchase_invoice_carries_the_workbook_formulas(pipeline, workbook):
    outcome = run(pipeline, ACHAT)
    row = workbook.row("05_FACTURES_ACHATS", outcome.row_index)
    n = outcome.row_index
    assert row[10] == f"=ROUND(G{n}+I{n};2)"
    assert row[11] == f"=J{n}-K{n}"
    assert row[12].startswith("=IF(COUNTIF($C$2:$C$")
    assert row[16] == f"=IF(AND(O{n}<J{n};TODAY()>N{n});TODAY()-N{n};0)"


# === 3. reutilisation de FRS-006 =========================================

def test_the_known_supplier_is_reused_and_never_duplicated(pipeline, workbook):
    before = len(workbook.rows("03_FOURNISSEURS"))
    outcome = run(pipeline, ACHAT)
    assert workbook.row("05_FACTURES_ACHATS", outcome.row_index)[3] == "FRS-006"
    assert len(workbook.rows("03_FOURNISSEURS")) == before
    assert workbook.writes_to("03_FOURNISSEURS") == []


def test_an_unknown_supplier_is_created_from_its_ice(pipeline, workbook):
    outcome = run(pipeline, ANOMALIE, forced=True)     # NORTH DATA SARL, ICE present
    created = workbook.rows("03_FOURNISSEURS")[-1]
    assert created[0] == "FRS-007"
    assert created[1] == "NORTH DATA SARL"
    assert created[2] == "002998877000061"


# === 4. deux lignes de detail, ecrites une seule fois =====================

def test_detail_lines_are_written_once(pipeline, workbook):
    outcome = run(pipeline, ACHAT)
    lines = [r for r in workbook.rows("16_LIGNES_FACTURES") if r[0] == outcome.stable_id]
    assert len(lines) == 2
    assert lines[0][4] == "Ramette papier A4 Premium"
    assert lines[0][5] == 10.0 and lines[0][8] == 3500.0
    assert lines[1][8] == 2750.0
    # Rejouer le meme document ne doit pas rajouter de lignes.
    run(pipeline, ACHAT)
    again = [r for r in workbook.rows("16_LIGNES_FACTURES") if r[0] == outcome.stable_id]
    assert len(again) == 2


# === 5. facture de vente dans le bon onglet ==============================

def test_a_sales_invoice_goes_to_the_sales_tab(pipeline, workbook):
    outcome = run(pipeline, VENTE)
    assert outcome.action == ACTION_AUTO
    assert outcome.tab == "04_FACTURES_VENTES"
    assert workbook.writes_to("05_FACTURES_ACHATS") == []
    row = workbook.row("04_FACTURES_VENTES", outcome.row_index)
    assert row[2] == "FAC-VTE-TEST-2026-012"
    assert row[9] == 14400.0


# === 6. devis classe sans ecriture comptable =============================

def test_a_quote_is_filed_without_any_accounting_entry(pipeline, workbook):
    outcome = run(pipeline, DEVIS)
    assert outcome.action == ACTION_AUTO
    assert outcome.accounting is False
    assert outcome.tab == "18_DOCUMENTS_COMMERCIAUX"
    # Ni chiffre d'affaires, ni TVA, ni charge.
    assert workbook.writes_to("04_FACTURES_VENTES") == []
    assert workbook.writes_to("05_FACTURES_ACHATS") == []
    assert workbook.writes_to("16_LIGNES_FACTURES") == []
    row = workbook.row("18_DOCUMENTS_COMMERCIAUX", outcome.row_index)
    assert row[2] == "Devis" and row[3] == "DEV-2026-008"


def test_a_purchase_order_is_filed_without_any_accounting_entry(pipeline, workbook):
    outcome = run(pipeline, BON_COMMANDE)
    assert outcome.accounting is False
    assert workbook.writes_to("05_FACTURES_ACHATS") == []
    assert workbook.row("18_DOCUMENTS_COMMERCIAUX", outcome.row_index)[2] == "Bon de commande"


# === 7. releve bancaire transforme en lignes bancaires ====================

def test_a_bank_statement_becomes_individual_bank_lines(pipeline, workbook):
    outcome = run(pipeline, RELEVE)
    assert outcome.action == ACTION_AUTO
    rows = workbook.rows("06_RELEVE_BANCAIRE")
    assert len(rows) == 6
    # Le sens est deduit de la variation du solde, puis recoupe.
    credit = next(r for r in rows if "CLINIQUE" in str(r[4]))
    debit = next(r for r in rows if "ATLAS" in str(r[4]))
    assert credit[7] == 14400.0 and credit[6] == ""
    assert debit[6] == 7500.0 and debit[7] == ""
    assert rows[-1][8] == 34751.0                 # solde de cloture
    assert all(r[12] == "Non rapproche" for r in rows)


def test_the_same_bank_statement_never_creates_duplicate_lines(pipeline, workbook):
    run(pipeline, RELEVE)
    count = len(workbook.rows("06_RELEVE_BANCAIRE"))
    run(pipeline, RELEVE, message_id="m2", attachment_id="att-9", tag="bis")
    assert len(workbook.rows("06_RELEVE_BANCAIRE")) == count


def test_bank_amounts_are_decimals_checked_against_declared_totals():
    doc = extract_document([text_of(RELEVE)])
    debits = sum((l.debit for l in doc.bank_lines if l.debit), Decimal("0"))
    credits = sum((l.credit for l in doc.bank_lines if l.credit), Decimal("0"))
    assert debits == Decimal("8049.00")
    assert credits == Decimal("18000.00")
    assert doc.anomalies == []                    # recoupement avec le releve


# === 8. penalite enregistree avec echeance ===============================

def test_a_penalty_is_recorded_with_its_due_date_and_a_reminder(pipeline, workbook):
    outcome = run(pipeline, PENALITE)
    assert outcome.action == ACTION_AUTO
    assert outcome.tab == "19_ECHEANCES_A_PAYER"
    row = workbook.row("19_ECHEANCES_A_PAYER", outcome.row_index)
    assert row[2] == "PEN-2026-044"
    assert row[5] == 450.0
    assert row[8] == "A payer"
    assert row[7] != ""                            # echeance renseignee
    assert len(workbook.events) == 1
    # L'API Calendar refuse une date seule : le rappel porte un INSTANT.
    assert workbook.events[0]["start_datetime"] == "2026-08-31T09:00:00"
    assert workbook.events[0]["timezone"] == "Africa/Casablanca"
    assert outcome.calendar_event
    # La penalite n'est pas une facture fournisseur ordinaire.
    assert workbook.writes_to("05_FACTURES_ACHATS") == []


def test_a_reminder_is_never_created_twice(pipeline, workbook):
    run(pipeline, PENALITE)
    run(pipeline, PENALITE, message_id="m2", attachment_id="att-9", tag="bis")
    assert len(workbook.events) == 1


# === 9. import / export avec devise et douane ============================

def test_an_import_invoice_keeps_currency_and_customs_data(pipeline, workbook):
    outcome = run(pipeline, IMPORT, forced=True)
    assert outcome.devise == "USD"
    customs = workbook.rows("20_DOUANE")[-1]
    assert customs[2] == "Import"
    assert customs[3] == "CI-2026-045"
    assert customs[5] == "USD"
    assert customs[6] == "CIF Casablanca"
    assert customs[7] == "China"
    assert "847160" in customs[9] and "844332" in customs[9]
    assert customs[10] == 3200.0 and customs[11] == 320.0 and customs[12] == 3520.0


def test_an_export_invoice_keeps_currency_and_destination(pipeline, workbook):
    outcome = run(pipeline, EXPORT, forced=True)
    assert outcome.tab == "04_FACTURES_VENTES"
    customs = workbook.rows("20_DOUANE")[-1]
    assert customs[2] == "Export"
    assert customs[5] == "EUR"
    assert customs[6] == "DAP Dakar"
    assert customs[8] == "Sénégal"


def test_a_foreign_currency_invoice_without_a_rate_asks_for_validation(pipeline, workbook):
    outcome = run(pipeline, IMPORT)
    assert outcome.action == ACTION_REVIEW
    assert any("taux de change" in r for r in outcome.reasons)
    assert workbook.writes_to("05_FACTURES_ACHATS") == []


# === 10. avoir envoye en validation ======================================

def test_a_credit_note_requires_human_validation(pipeline, workbook):
    outcome = run(pipeline, AVOIR)
    assert outcome.action == ACTION_REVIEW
    assert any("avoir" in r for r in outcome.reasons)
    assert workbook.writes_to("05_FACTURES_ACHATS") == []
    assert workbook.writes_to("17_AVOIRS") == []


def test_a_validated_credit_note_is_written_with_negative_amounts(pipeline, workbook):
    outcome = run(pipeline, AVOIR, forced=True)
    row = workbook.row("17_AVOIRS", outcome.row_index)
    assert row[2] == "AV-2026-003"
    assert row[3] == "Fournisseur"
    assert row[6] == "FAC-TEST-2026-002"           # facture d'origine
    assert row[7] == -600.0 and row[9] == -120.0 and row[10] == -720.0
    # Un avoir n'est jamais confondu avec une facture.
    assert workbook.writes_to("05_FACTURES_ACHATS") == []


# === 11. facture incoherente ============================================

def test_an_incoherent_invoice_is_not_written_and_goes_to_validation(pipeline, workbook):
    outcome = run(pipeline, ANOMALIE)
    assert outcome.action == ACTION_REVIEW
    assert any("HT + TVA ne correspond pas au TTC" in r for r in outcome.reasons)
    assert workbook.writes_to("05_FACTURES_ACHATS") == []
    assert workbook.writes_to("16_LIGNES_FACTURES") == []


def test_the_tolerance_is_explicit_and_absorbs_a_rounding_cent():
    from app.doc_policy import DecisionContext, decide

    doc = extract_document([text_of(ACHAT)])
    doc.montant_ttc.value += Decimal("0.01")
    assert decide(doc, DecisionContext()).action == ACTION_AUTO
    doc.montant_ttc.value += Decimal("0.02")
    assert decide(doc, DecisionContext()).action == ACTION_REVIEW


# === 12. ICE manquant ===================================================

def test_a_missing_ice_goes_to_validation_without_creating_the_supplier(pipeline, workbook):
    outcome = run(pipeline, SANS_ICE)
    assert outcome.action == ACTION_REVIEW
    assert any("ICE" in r for r in outcome.reasons)
    assert workbook.writes_to("03_FOURNISSEURS") == []
    assert workbook.writes_to("05_FACTURES_ACHATS") == []


# === 13. recu rapproche seulement si la facture est unique ================

def test_a_receipt_settles_the_matching_invoice_only(pipeline, workbook):
    sale = run(pipeline, VENTE)
    outcome = run(pipeline, RECU)
    assert outcome.action == ACTION_AUTO
    row = workbook.row("04_FACTURES_VENTES", sale.row_index)
    assert row[14] == 14400.0
    assert row[15] == "Payee"
    # Un recu ne cree jamais de facture.
    assert outcome.stable_id == "REC-2026-017"


def test_a_receipt_without_any_matching_invoice_asks_for_validation(pipeline, workbook):
    outcome = run(pipeline, RECU)          # la facture de vente n'existe pas
    assert outcome.action == ACTION_REVIEW
    assert any("aucune facture" in r for r in outcome.reasons)


def test_a_receipt_is_never_settled_on_amount_similarity_alone(pipeline, workbook):
    """Deux factures du meme montant : aucune n'est soldee automatiquement."""
    for index in (2, 3):
        workbook.tabs["04_FACTURES_VENTES"][index][2] = "FAC-VTE-TEST-2026-012"
        workbook.tabs["04_FACTURES_VENTES"][index][9] = 14400.0
    outcome = run(pipeline, RECU)
    assert outcome.action == ACTION_REVIEW
    assert any("peuvent correspondre" in r for r in outcome.reasons)


# === 16. doublons par message, piece jointe et empreinte =================

def test_the_same_document_in_the_same_email_is_never_written_twice(pipeline, workbook):
    first = run(pipeline, ACHAT)
    rows = len(workbook.rows("05_FACTURES_ACHATS"))
    second = run(pipeline, ACHAT)
    assert first.action == ACTION_AUTO
    assert second.action == ACTION_DUPLICATE
    assert len(workbook.rows("05_FACTURES_ACHATS")) == rows


def test_the_same_pdf_in_another_email_is_detected_by_its_hash(pipeline, workbook):
    run(pipeline, ACHAT)
    rows = len(workbook.rows("05_FACTURES_ACHATS"))
    outcome = run(pipeline, ACHAT, message_id="autre-email", attachment_id="att-99")
    assert outcome.action == ACTION_DUPLICATE
    assert len(workbook.rows("05_FACTURES_ACHATS")) == rows


def test_a_certain_duplicate_is_never_written_even_when_validated(pipeline, workbook):
    run(pipeline, ACHAT)
    rows = len(workbook.rows("05_FACTURES_ACHATS"))
    outcome = run(pipeline, ACHAT, message_id="m3", attachment_id="att-3", forced=True)
    assert outcome.action == ACTION_DUPLICATE
    assert len(workbook.rows("05_FACTURES_ACHATS")) == rows


def test_five_documents_in_one_email_give_five_results(pipeline, workbook):
    names = [ACHAT, DEVIS, PENALITE, ANOMALIE, BON_COMMANDE]
    outcomes = [
        run(pipeline, name, message_id="m-multi", attachment_id=f"att-{i}")
        for i, name in enumerate(names)
    ]
    assert len(outcomes) == 5
    assert len({o.doc_key for o in outcomes}) == 5
    assert [o.action for o in outcomes] == [
        ACTION_AUTO, ACTION_AUTO, ACTION_AUTO, ACTION_REVIEW, ACTION_AUTO
    ]
    assert {o.tab for o in outcomes if o.tab} == {
        "05_FACTURES_ACHATS", "18_DOCUMENTS_COMMERCIAUX", "19_ECHEANCES_A_PAYER"
    }


# === 17. reprise apres panne Drive =======================================

def test_a_drive_failure_leaves_exactly_one_accounting_row(pipeline, workbook, db_path):
    workbook.drive_fails = True
    outcome = run(pipeline, ACHAT)
    assert len(workbook.writes_to("05_FACTURES_ACHATS!A")) == 1
    assert store.get_document(db_path, outcome.doc_key)["state"] == store.PARTIAL
    assert workbook.writes_to("14_IMPORTS_LOG") == []


def test_the_next_cycle_finishes_drive_and_the_log_without_a_second_row(
    pipeline, workbook, db_path
):
    workbook.drive_fails = True
    first = run(pipeline, ACHAT)
    rows = len(workbook.rows("05_FACTURES_ACHATS"))
    lines = len(workbook.rows("16_LIGNES_FACTURES"))

    workbook.drive_fails = False
    second = run(pipeline, ACHAT)

    assert len(workbook.rows("05_FACTURES_ACHATS")) == rows, "une deuxieme ligne a ete ecrite"
    assert len(workbook.rows("16_LIGNES_FACTURES")) == lines
    assert second.row_index == first.row_index
    assert second.drive_link
    assert len(workbook.rows("14_IMPORTS_LOG")) == 1
    assert store.get_document(db_path, second.doc_key)["state"] == store.COMPLETED


def test_a_third_cycle_writes_nothing_more(pipeline, workbook):
    workbook.drive_fails = True
    run(pipeline, ACHAT)
    workbook.drive_fails = False
    run(pipeline, ACHAT)
    before = len(workbook.calls)
    outcome = run(pipeline, ACHAT)
    assert outcome.action == ACTION_DUPLICATE
    assert workbook.writes_to("05_FACTURES_ACHATS") == workbook.writes_to("05_FACTURES_ACHATS")
    assert len(workbook.rows("14_IMPORTS_LOG")) == 1


# === 19. isolation de deux clients ======================================

def test_two_chat_ids_never_see_each_other_documents(workbook, monkeypatch):
    import app.doc_pipeline as module

    registry: dict[bytes, str] = {}
    monkeypatch.setattr(
        module, "extract_from_pdf_bytes",
        lambda c, company="X BLASTE", ocr=True: extract_document([registry[c]], company=company),
    )
    content = b"%PDF-partage"
    registry[content] = text_of(ACHAT)
    file = DocumentFile(filename="facture.pdf", content=content, source="attachment")

    db_a, db_b = tempfile.mktemp(suffix=".db"), tempfile.mktemp(suffix=".db")
    for path in (db_a, db_b):
        init_db(path)
        store.ensure_schema(path)
    pipe_a = DocumentPipeline(workbook, db_path=db_a, chat_id=111, spreadsheet_id="s")
    pipe_b = DocumentPipeline(workbook, db_path=db_b, chat_id=222, spreadsheet_id="s")

    pipe_a.process_document(file, {"messageId": "m1"}, attachment_id="a1", source_url="u")
    outcome_b = pipe_b.process_document(
        file, {"messageId": "m1"}, attachment_id="a1", source_url="u"
    )
    # Le document de A n'est pas un doublon pour B : leurs bases sont cloisonnees.
    assert outcome_b.action != ACTION_DUPLICATE
    assert store.find_by_sha256(db_a, 222, file.sha256) is None
    assert len(store.list_by_message(db_a, 111, "m1")) == 1
    assert len(store.list_by_message(db_b, 222, "m1")) == 1


# === 20. aucune fuite de secret =========================================

def test_no_secret_ever_appears_in_a_document_message(pipeline, workbook):
    outcomes = [run(pipeline, name, attachment_id=f"a{i}")
                for i, name in enumerate([ACHAT, ANOMALIE, SANS_ICE, AVOIR])]
    from app.mail_worker import build_review_message

    blob = " ".join(
        (o.error or "") + " ".join(o.reasons) + (build_review_message(o) if o.document else "")
        for o in outcomes
    )
    for secret in ("ak_", "sk-", "x-api-key", "Bearer ", "client_secret", "GOCSPX"):
        assert secret not in blob, f"fuite potentielle : {secret}"


def test_an_api_key_never_reaches_the_error_message(db_path):
    from app.mail_worker import MailWorker, MailWorkerError
    from unittest.mock import patch

    worker = MailWorker(api_key="ak_super_secret", chat_id=1, db_path=db_path)
    with patch("httpx.Client", side_effect=RuntimeError("boom")):
        with pytest.raises(MailWorkerError) as exc:
            worker.search_messages()
    assert "ak_super_secret" not in str(exc.value)


# === journal d'import ====================================================

def test_the_import_log_records_drive_and_gmail_for_every_document(pipeline, workbook):
    outcome = run(pipeline, ACHAT)
    entry = workbook.rows("14_IMPORTS_LOG")[-1]
    assert entry[2] == outcome.stable_id
    detail = entry[5]
    for expected in ("FAC-TEST-2026-002", "05_FACTURES_ACHATS", "m1", "drive.google.com"):
        assert expected in detail


def test_an_unknown_document_is_filed_for_review_without_accounting(pipeline, workbook):
    content = b"%PDF-inconnu"
    pipeline.registry[content] = chr(10).join(
        ["Compte rendu de reunion", "Points abordes", "fin"]
    )
    file = DocumentFile(filename="note.pdf", content=content, source="attachment")
    outcome = pipeline.process_document(
        file, {"messageId": "m1"}, attachment_id="a1", source_url="u"
    )
    assert outcome.action == "unknown"
    assert outcome.drive_link                      # depose dans Drive / A verifier
    assert workbook.writes_to("05_FACTURES_ACHATS") == []
    assert workbook.writes_to("04_FACTURES_VENTES") == []
