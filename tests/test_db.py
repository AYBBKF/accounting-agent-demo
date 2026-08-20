"""Tests de la persistance SQLite (base interne principale) et des ID
stables utilises pour la synchronisation idempotente vers Google Sheets."""
from datetime import date
from decimal import Decimal

from app.db import (
    connect,
    init_db,
    save_bank_lines,
    save_invoices,
    stable_bank_line_id,
    stable_invoice_id,
)
from app.demo_data import DemoBankLine, DemoInvoice


def _invoice(numero="DEMO-1"):
    return DemoInvoice(
        fournisseur="Fournisseur DEMO",
        numero=numero,
        date_facture=date(2026, 1, 10),
        montant_ht=Decimal("100.00"),
        taux_tva=Decimal("20"),
    )


def test_save_invoices_persists_and_returns_stable_ids(tmp_path):
    db_path = str(tmp_path / "demo.db")
    init_db(db_path)

    ids = save_invoices(db_path, chat_id=999653395, invoices=[_invoice()])

    assert len(ids) == 1
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT chat_id, numero, montant_ht, taux_tva, montant_tva, montant_ttc FROM demo_invoices"
        ).fetchone()
    assert row == ("999653395", "DEMO-1", "100.00", "20", "20.00", "120.00")
    assert stable_invoice_id(999653395, ids[0]) == f"INV-999653395-{ids[0]}"


def test_save_invoices_is_idempotent_per_chat_never_duplicates(tmp_path):
    db_path = str(tmp_path / "demo.db")
    init_db(db_path)

    save_invoices(db_path, chat_id=1, invoices=[_invoice("A"), _invoice("B")])
    save_invoices(db_path, chat_id=1, invoices=[_invoice("A"), _invoice("B")])

    with connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM demo_invoices WHERE chat_id = '1'").fetchone()[0]
    assert count == 2


def test_save_bank_lines_persists_and_returns_stable_ids(tmp_path):
    db_path = str(tmp_path / "demo.db")
    init_db(db_path)
    line = DemoBankLine(date_operation=date(2026, 1, 11), libelle="VIR DEMO", montant=Decimal("-120.00"))

    ids = save_bank_lines(db_path, chat_id=42, bank_lines=[line])

    assert len(ids) == 1
    assert stable_bank_line_id(42, ids[0]) == f"BANK-42-{ids[0]}"


def test_different_chats_do_not_overwrite_each_other(tmp_path):
    db_path = str(tmp_path / "demo.db")
    init_db(db_path)

    save_invoices(db_path, chat_id=1, invoices=[_invoice("A")])
    save_invoices(db_path, chat_id=2, invoices=[_invoice("B")])

    with connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM demo_invoices").fetchone()[0]
    assert total == 2
