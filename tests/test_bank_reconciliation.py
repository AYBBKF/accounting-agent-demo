"""Rapprochement bancaire : reference + montant + sens, ou rien.

Une correspondance n'est retenue que si les TROIS concordent. Un debit ne
peut solder qu'une facture d'achat, un credit qu'une facture de vente :
sans cette regle, un encaissement client aurait pu solder une facture
fournisseur du meme montant.
"""
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from app import doc_store as store
from app.db import init_db
from app.doc_extract import BankLine, ExtractedDocument
from app.doc_pipeline import DocumentPipeline
from app.doc_types import BANK_STATEMENT, Classification
from tests.workbook_fake import FakeWorkbook

RATES = (Decimal("0"), Decimal("7"), Decimal("10"), Decimal("20"))


@pytest.fixture
def db_path():
    path = tempfile.mktemp(suffix=".db")
    init_db(path)
    store.ensure_schema(path)
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def pipeline(db_path):
    workbook = FakeWorkbook()
    pipe = DocumentPipeline(
        workbook, db_path=db_path, chat_id=999653395, spreadsheet_id="s",
        allowed_vat_rates=RATES,
    )
    # Deux factures deja comptabilisees, comme dans le classeur reel.
    pipe._write("05_FACTURES_ACHATS!A2:Q2", [[
        "FA-2026-001", "2026-08-15", "F2026-1101", "FRS-001", "ATLAS PRO SARL",
        "Import", 5000.0, 20.0, 1000.0, 6000.0, "", "", "", "2026-09-15", "", 0, "",
    ]])
    pipe._write("04_FACTURES_VENTES!A2:Q2", [[
        "FV-2026-001", "2026-08-20", "V2026-1151", "CLI-001", "NOVA SARL",
        "Import", 8000.0, 20.0, 1600.0, 9600.0, "", "", "", "2026-09-20", "", 0, "",
    ]])
    return pipe, workbook


def statement(lines):
    doc = ExtractedDocument(classification=Classification(BANK_STATEMENT, 0.98, "", []))
    doc.bank_lines = lines
    return doc


def line(libelle, reference, debit=None, credit=None):
    from datetime import date

    return BankLine(
        date_operation=date(2026, 8, 16), libelle=libelle, reference=reference,
        debit=debit, credit=credit, solde=None, devise="MAD", page=1,
    )


def test_a_debit_reconciles_a_purchase_invoice(pipeline):
    pipe, workbook = pipeline
    doc = statement([line("VIR ATLAS PRO - F2026-1101", "F2026-1101", debit=Decimal("6000.00"))])
    pipe.write_bank_statement(doc, doc_key="d1")
    rapproches = pipe.reconcile_bank_lines(doc, first_row=2)
    assert len(rapproches) == 1
    assert rapproches[0]["numero"] == "F2026-1101"
    assert rapproches[0]["sens"] == "Debit"
    assert workbook.rows("08_RAPPROCHEMENT")


def test_a_credit_reconciles_a_sales_invoice(pipeline):
    pipe, _ = pipeline
    doc = statement([line("REGLEMENT NOVA - V2026-1151", "V2026-1151", credit=Decimal("9600.00"))])
    pipe.write_bank_statement(doc, doc_key="d2")
    rapproches = pipe.reconcile_bank_lines(doc, first_row=2)
    assert [r["numero"] for r in rapproches] == ["V2026-1151"]


def test_the_reconciled_invoice_is_marked_paid(pipeline):
    pipe, workbook = pipeline
    doc = statement([line("VIR ATLAS PRO - F2026-1101", "F2026-1101", debit=Decimal("6000.00"))])
    pipe.write_bank_statement(doc, doc_key="d3")
    pipe.reconcile_bank_lines(doc, first_row=2)
    ligne = workbook.row("05_FACTURES_ACHATS", 2)
    assert ligne[14] == 6000.0 and ligne[15] == "Payee"   # colonnes O et P


def test_the_bank_row_records_the_matched_invoice(pipeline):
    pipe, workbook = pipeline
    doc = statement([line("VIR ATLAS PRO - F2026-1101", "F2026-1101", debit=Decimal("6000.00"))])
    pipe.write_bank_statement(doc, doc_key="d4")
    pipe.reconcile_bank_lines(doc, first_row=2)
    ligne = workbook.row("06_RELEVE_BANCAIRE", 2)
    assert ligne[11] == "F2026-1101" and ligne[12] == "Rapproche"


def test_a_wrong_amount_reconciles_nothing(pipeline):
    pipe, _ = pipeline
    doc = statement([line("VIR ATLAS PRO - F2026-1101", "F2026-1101", debit=Decimal("10.00"))])
    pipe.write_bank_statement(doc, doc_key="d5")
    assert pipe.reconcile_bank_lines(doc, first_row=2) == []


def test_a_credit_never_settles_a_purchase_invoice(pipeline):
    """Le SENS fait partie du rapprochement : sans lui, 6 000 encaisses
    auraient solde une facture fournisseur de 6 000."""
    pipe, _ = pipeline
    doc = statement([line("ENCAISSEMENT - F2026-1101", "F2026-1101", credit=Decimal("6000.00"))])
    pipe.write_bank_statement(doc, doc_key="d6")
    assert pipe.reconcile_bank_lines(doc, first_row=2) == []


def test_an_operation_without_reference_reconciles_nothing(pipeline):
    pipe, _ = pipeline
    doc = statement([line("FRAIS TENUE DE COMPTE", "", debit=Decimal("120.00"))])
    pipe.write_bank_statement(doc, doc_key="d7")
    assert pipe.reconcile_bank_lines(doc, first_row=2) == []


def test_an_undetermined_direction_reconciles_nothing(pipeline):
    pipe, _ = pipeline
    op = line("VIR ATLAS PRO - F2026-1101", "F2026-1101")
    op.mouvement = Decimal("6000.00")
    doc = statement([op])
    pipe.write_bank_statement(doc, doc_key="d8")
    assert pipe.reconcile_bank_lines(doc, first_row=2) == []


def test_reconciling_twice_never_duplicates(pipeline):
    """Idempotence : rejouer le meme releve n'ajoute aucune ligne."""
    pipe, workbook = pipeline
    doc = statement([line("VIR ATLAS PRO - F2026-1101", "F2026-1101", debit=Decimal("6000.00"))])
    pipe.write_bank_statement(doc, doc_key="d9")
    pipe.reconcile_bank_lines(doc, first_row=2)
    avant = len(workbook.rows("06_RELEVE_BANCAIRE"))
    ecrites, _ = pipe.write_bank_statement(doc, doc_key="d9")
    assert ecrites == 0
    assert len(workbook.rows("06_RELEVE_BANCAIRE")) == avant
