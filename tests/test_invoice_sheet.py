"""Conventions REELLES du classeur : identifiants, nombres, formats.

Ces tests fixent ce qui avait ete constate faux sur la ligne 14 :
un numero de facture ecrit en colonne A, un ID fournisseur vide, des
montants en chaines "4000.00 MAD", un taux de TVA a 0,2 en format
pourcentage, un statut "NON PAYEE" hors de la liste de validation, et
des colonnes de formules laissees vides.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.invoice_pdf import InvoiceLine
from app.invoice_sheet import (
    InvoiceSheetError,
    build_import_log_row,
    build_line_rows,
    build_row_plan,
    next_stable_invoice_id,
    next_supplier_id,
    normalize_status,
    normalize_vat_rate,
    to_serial,
)

EXISTING_IDS = [f"FA-2026-{i:03d}" for i in range(1, 13)]


# --- identifiants stables -------------------------------------------------

def test_the_next_invoice_id_continues_the_workbook_sequence():
    assert next_stable_invoice_id(EXISTING_IDS, 2026) == "FA-2026-013"


def test_the_invoice_id_is_never_the_invoice_number():
    ids = EXISTING_IDS + ["FAC-TEST-2026-001"]
    assert next_stable_invoice_id(ids, 2026) == "FA-2026-013"


def test_the_sequence_does_not_restart_when_the_year_changes():
    assert next_stable_invoice_id(EXISTING_IDS, 2027) == "FA-2027-013"


def test_the_first_invoice_of_an_empty_workbook_is_numbered_one():
    assert next_stable_invoice_id([], 2026) == "FA-2026-001"


def test_the_next_supplier_id_continues_the_supplier_sequence():
    assert next_supplier_id([f"FRS-{i:03d}" for i in range(1, 6)]) == "FRS-006"
    assert next_supplier_id([]) == "FRS-001"


# --- dates ----------------------------------------------------------------

def test_dates_are_written_as_google_sheets_serial_numbers():
    # Valeurs relues du classeur : 2026-08-21 -> 46255, 2026-09-20 -> 46285.
    assert to_serial(date(2026, 8, 21)) == 46255
    assert to_serial(date(2026, 9, 20)) == 46285


# --- taux de TVA ----------------------------------------------------------

def test_the_vat_rate_uses_the_workbook_format_not_a_percentage_fraction():
    assert normalize_vat_rate(Decimal("20")) == Decimal("20")
    assert normalize_vat_rate(Decimal("0.20")) == Decimal("20.00")
    assert normalize_vat_rate(None) is None


# --- statut ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("NON PAYEE", "Impayee"),
        ("Non payée", "Impayee"),
        ("Impayee", "Impayee"),
        ("Payee", "Payee"),
        ("Réglée", "Payee"),
        ("Partiellement payee", "Partiellement payee"),
        ("", "Impayee"),
        (None, "Impayee"),
    ],
)
def test_status_is_mapped_to_the_workbook_validation_list(raw, expected):
    assert normalize_status(raw, montant_paye=None) == expected


def test_an_unpaid_invoice_is_impayee_whatever_the_pdf_says():
    assert normalize_status("Payee", montant_paye=Decimal("0")) == "Impayee"


# --- ligne complete -------------------------------------------------------

def _plan(**overrides):
    kwargs = dict(
        tab="05_FACTURES_ACHATS",
        row_index=14,
        stable_id="FA-2026-013",
        supplier_id="FRS-006",
        supplier_name="ATLAS BUREAU SARL",
        numero="FAC-TEST-2026-001",
        description="Import email - facture.pdf",
        date_facture=date(2026, 8, 21),
        date_echeance=date(2026, 9, 20),
        montant_ht=Decimal("4000.00"),
        taux_tva=Decimal("20"),
        montant_tva=Decimal("800.00"),
        montant_ttc=Decimal("4800.00"),
        statut="NON PAYEE",
    )
    kwargs.update(overrides)
    return build_row_plan(**kwargs)


def test_amounts_are_native_numbers_never_strings_containing_mad():
    row = _plan().values_a_j
    for index in (6, 8, 9):
        assert isinstance(row[index], float)
        assert "MAD" not in str(row[index])
    assert row[6] == 4000.0 and row[8] == 800.0 and row[9] == 4800.0


def test_column_a_holds_the_stable_id_and_column_d_the_supplier_id():
    row = _plan().values_a_j
    assert row[0] == "FA-2026-013"
    assert row[2] == "FAC-TEST-2026-001"
    assert row[3] == "FRS-006"


def test_the_formula_columns_are_filled_and_reference_the_right_row():
    plan = _plan()
    assert plan.formulas_k_m == [
        "=ROUND(G14+I14;2)",
        "=J14-K14",
        '=IF(COUNTIF($C$2:$C$14;C14)>1;"DOUBLON";"")',
    ]
    assert plan.formula_q == "=IF(AND(O14<J14;TODAY()>N14);TODAY()-N14;0)"


def test_the_duplicate_formula_range_covers_the_new_row_without_touching_others():
    # La plage doit aller jusqu'a la ligne ecrite, sinon la nouvelle facture
    # ne serait jamais comptee dans son propre controle de doublon.
    plan = _plan(row_index=20, last_data_row=20)
    assert "$C$2:$C$20" in plan.formulas_k_m[2]


def test_writing_over_the_header_row_is_refused():
    with pytest.raises(InvoiceSheetError):
        _plan(row_index=1)


def test_the_ranges_target_exactly_one_row():
    plan = _plan()
    assert plan.range_a_j == "05_FACTURES_ACHATS!A14:J14"
    assert plan.range_n_p == "05_FACTURES_ACHATS!N14:P14"
    assert plan.range_k_m == "05_FACTURES_ACHATS!K14:M14"
    assert plan.range_q == "05_FACTURES_ACHATS!Q14"


# --- lignes de detail -----------------------------------------------------

def test_detail_lines_are_linked_to_the_invoice_id():
    lignes = [
        InvoiceLine("Ramettes papier A4 premium", Decimal("2"), Decimal("750.00"),
                    Decimal("20"), Decimal("1500.00")),
        InvoiceLine("Imprimante laser professionnelle", Decimal("1"), Decimal("2500.00"),
                    Decimal("20"), Decimal("2500.00")),
    ]
    rows = build_line_rows(
        stable_id="FA-2026-013", tab="05_FACTURES_ACHATS",
        numero="FAC-TEST-2026-001", lignes=lignes,
    )
    assert [r[0] for r in rows] == ["FA-2026-013", "FA-2026-013"]
    assert [r[3] for r in rows] == [1, 2]
    assert rows[0][5] == 2.0 and rows[0][8] == 1500.0
    assert all(isinstance(r[8], float) for r in rows)


# --- journal d'import -----------------------------------------------------

def test_the_import_log_row_matches_the_six_existing_columns():
    row = build_import_log_row(
        horodatage="2026-08-22T10:00:00+00:00",
        stable_id="FA-2026-013",
        action="Créé",
        statut="Importée automatiquement",
        numero="FAC-TEST-2026-001",
        fournisseur="ATLAS BUREAU SARL",
        ice="002345678000043",
        montant_ht=Decimal("4000.00"),
        montant_tva=Decimal("800.00"),
        montant_ttc=Decimal("4800.00"),
        tab="05_FACTURES_ACHATS",
        row_index=14,
        gmail_message_id="1a0263e63516623c",
        gmail_expediteur="expediteur@example.com",
        gmail_objet="[XBLASTE] Facture test",
        piece_jointe="facture.pdf",
        drive_lien="https://drive.google.com/file/d/abc/view",
    )
    assert len(row) == 6
    assert row[2] == "FA-2026-013"
    detail = row[5]
    for expected in ("002345678000043", "05_FACTURES_ACHATS ligne 14",
                     "1a0263e63516623c", "facture.pdf", "drive.google.com"):
        assert expected in detail
