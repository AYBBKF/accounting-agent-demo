"""Releve bancaire : le sens vient de la COLONNE, jamais d'une supposition.

Constate en reel : un releve dont chaque operation n'expose qu'UN montant
(colonnes Debit / Credit, sans solde courant) voyait ce montant recopie
dans la colonne "Solde (releve)". Le classeur affirmait donc un solde que
le document n'annonce nulle part, et, faute de sens, aucun rapprochement
n'etait produit.
"""
from decimal import Decimal

import pytest

from app.doc_extract import (
    Cell,
    bank_reference,
    extract_bank_lines_by_column,
)
from app.doc_routing import build_bank_rows
from app.doc_extract import BankLine, ExtractedDocument
from app.doc_types import BANK_STATEMENT, Classification


def cells(rows):
    """(y, [(x, texte)]) -> cellules positionnees."""
    out = []
    for y, contenu in rows:
        for x, texte in contenu:
            out.append(Cell(page=1, x=float(x), y=float(y), text=texte))
    return out


# Positions REELLES relevees dans le PDF du lot : Debit x=340.5, Credit x=425.5
RELEVE = cells([
    (151.8, [(6.0, "Date"), (82.5, "Libelle / Reference"), (340.5, "Debit"), (425.5, "Credit")]),
    (123.8, [(6.0, "16/08/2026"), (82.5, "VIR ATLAS PRO - F2026-1101"), (361.1, "6 000,00 MAD")]),
    (95.8, [(6.0, "18/08/2026"), (82.5, "VIR MAGHREB TECH - F2026-1102"), (361.1, "3 840,00 MAD")]),
    (67.8, [(6.0, "21/08/2026"), (82.5, "REGLEMENT CLIENT NOVA - V2026-1151"), (446.2, "9 600,00 MAD")]),
    (39.8, [(6.0, "22/08/2026"), (82.5, "FRAIS TENUE DE COMPTE"), (368.0, "120,00 MAD")]),
])


def lignes():
    operations, _ = extract_bank_lines_by_column(RELEVE, "MAD")
    return operations


def test_the_column_decides_the_direction():
    ops = lignes()
    assert len(ops) == 4
    assert ops[0].debit == Decimal("6000.00") and ops[0].credit is None
    assert ops[2].credit == Decimal("9600.00") and ops[2].debit is None


def test_a_single_amount_is_never_recorded_as_a_balance():
    """Aucune de ces operations n'annonce de solde : la colonne reste vide."""
    for op in lignes():
        assert op.solde is None, op.libelle


def test_the_written_row_never_carries_an_invented_balance():
    doc = ExtractedDocument(classification=Classification(BANK_STATEMENT, 0.98, "", []))
    doc.bank_lines = lignes()
    for row in build_bank_rows(start_index=1, doc=doc):
        assert row[8] == "", "colonne Solde : rien ne doit y etre ecrit sans preuve"


def test_the_invoice_reference_is_read_from_the_label():
    ops = lignes()
    assert ops[0].reference == "F2026-1101"
    assert ops[2].reference == "V2026-1151"
    assert ops[3].reference == ""          # "FRAIS TENUE DE COMPTE" n'en cite aucune


def test_a_reference_with_letters_and_dashes_is_still_read():
    assert bank_reference("VIR REL-BP-2026-08") == "REL-BP-2026-08"
    assert bank_reference("REGLEMENT AV2026-1171") == "AV2026-1171"
    assert bank_reference("FRAIS DIVERS") == ""


def test_an_amount_outside_the_columns_stays_a_movement_to_validate():
    """Sens indetermine : le montant est conserve, mais jamais en solde."""
    hors = cells([
        (151.8, [(6.0, "Date"), (82.5, "Libelle"), (340.5, "Debit"), (425.5, "Credit")]),
        (123.8, [(6.0, "16/08/2026"), (82.5, "VIR DIVERS"), (120.0, "500,00 MAD")]),
    ])
    ops, anomalies = extract_bank_lines_by_column(hors, "MAD")
    assert len(ops) == 1
    op = ops[0]
    assert op.debit is None and op.credit is None
    assert op.solde is None
    assert op.mouvement == Decimal("500.00")
    assert op.sens_indetermine
    assert any("sens a valider" in a.lower() for a in anomalies)


def test_the_uncertain_movement_is_visible_in_the_row():
    hors = cells([
        (151.8, [(6.0, "Date"), (82.5, "Libelle"), (340.5, "Debit"), (425.5, "Credit")]),
        (123.8, [(6.0, "16/08/2026"), (82.5, "VIR DIVERS"), (120.0, "500,00 MAD")]),
    ])
    ops, _ = extract_bank_lines_by_column(hors, "MAD")
    doc = ExtractedDocument(classification=Classification(BANK_STATEMENT, 0.98, "", []))
    doc.bank_lines = ops
    row = build_bank_rows(start_index=1, doc=doc)[0]
    assert row[6] == "" and row[7] == "" and row[8] == ""
    assert "valider" in row[12].lower() and "500" in row[12]


def test_one_uncertain_operation_does_not_block_the_others():
    melange = cells([
        (151.8, [(6.0, "Date"), (82.5, "Libelle"), (340.5, "Debit"), (425.5, "Credit")]),
        (123.8, [(6.0, "16/08/2026"), (82.5, "VIR ATLAS PRO - F2026-1101"), (361.1, "6 000,00 MAD")]),
        (95.8, [(6.0, "17/08/2026"), (82.5, "VIR DIVERS"), (120.0, "500,00 MAD")]),
        (67.8, [(6.0, "21/08/2026"), (82.5, "REGLEMENT NOVA - V2026-1151"), (446.2, "9 600,00 MAD")]),
    ])
    ops, _ = extract_bank_lines_by_column(melange, "MAD")
    assert len(ops) == 3
    assert ops[0].debit == Decimal("6000.00")
    assert ops[1].sens_indetermine
    assert ops[2].credit == Decimal("9600.00")


def test_a_statement_without_debit_credit_headers_falls_back():
    """Sans en-tetes exploitables, la lecture par colonne ne devine rien."""
    ops, anomalies = extract_bank_lines_by_column(
        cells([(100.0, [(6.0, "16/08/2026"), (82.5, "VIR"), (300.0, "10,00 MAD")])]), "MAD"
    )
    assert ops == [] and anomalies == []
