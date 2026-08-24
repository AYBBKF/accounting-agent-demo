"""Les documents REELS du Pack V2, tels qu'ils sont sortis de Drive.

Les fixtures de `test_validation_v2_regression.py` etaient ecrites a la
main. Elles passaient toutes alors que la production, elle, ecrivait encore
FAC-V2-AMB-002 automatiquement : le document reel annonce
"MONTANT A PAYER 2 600.00" et pas "Net a payer", libelle que ma liste de
synonymes ne contenait pas.

Ce module part donc du texte REELLEMENT extrait des PDF archives dans
Drive. Une liste de libelles est toujours incomplete ; la regle correcte
reconnait un total final a sa FORME.
"""
from pathlib import Path

import pytest

from app.doc_extract import extract_document
from app.doc_policy import ACTION_REVIEW, ACTION_UNKNOWN, decide
from app.doc_types import (
    PAYMENT_RECEIPT,
    PURCHASE_INVOICE,
    SUPPLIER_CREDIT_NOTE,
    UNKNOWN,
)

PACK = Path(__file__).parent / "fixtures" / "packv2"


def document(name: str):
    return extract_document(
        [(PACK / f"{name}.txt").read_text(encoding="utf-8")], company="X BLASTE"
    )


def motifs(doc) -> str:
    return " | ".join(decide(doc).reasons)


@pytest.mark.parametrize(
    "name",
    [
        "FAC-V2-AMB-001",
        "FAC-V2-AMB-002",
        "FAC-V2-INC-001",
        "FAC-V2-USD-001",
        "AV-V2-INC-001",
    ],
)
def test_no_problem_document_is_ever_written_automatically(name):
    doc = document(name)
    assert decide(doc).action == ACTION_REVIEW, motifs(doc)


def test_two_final_amounts_are_seen_whatever_the_label():
    """Le cas qui a echappe en production : 'MONTANT A PAYER' 2 600 face a
    'Total TTC' 2 400."""
    doc = document("FAC-V2-AMB-002")
    assert "montant_ttc" in doc.ambigus
    assert any("plusieurs valeurs possibles" in r for r in decide(doc).reasons)


def test_a_missing_total_is_never_invented():
    doc = document("FAC-V2-INC-001")
    assert doc.montant_ttc is None
    assert doc.doc_type == PURCHASE_INVOICE


def test_a_foreign_currency_invoice_is_never_read_as_dirhams():
    doc = document("FAC-V2-USD-001")
    assert doc.devise == "USD"
    reasons = decide(doc).reasons
    assert any("taux de change" in r for r in reasons)
    # Le titre "FACTURE FOURNISSEUR EN DEVISE" avait fait lire "EN" comme
    # raison sociale, et une fiche fournisseur FRS-014 avait ete creee.
    assert any("raison sociale" in r for r in reasons)


def test_a_credit_note_without_origin_stays_unimputed():
    doc = document("AV-V2-INC-001")
    assert doc.doc_type == SUPPLIER_CREDIT_NOTE
    assert doc.facture_liee is None
    assert any("facture d'origine" in r for r in decide(doc).reasons)


def test_the_maintenance_contract_produces_no_accounting_entry():
    doc = document("CTR-V2-2026-009")
    assert doc.doc_type == UNKNOWN
    assert doc.doc_type != PAYMENT_RECEIPT
    decision = decide(doc)
    assert decision.action == ACTION_UNKNOWN
    assert not decision.writes_accounting


def test_the_ttc_discrepancy_is_still_caught():
    doc = document("FAC-V2-AMB-001")
    assert any("HT + TVA" in r for r in decide(doc).reasons)
