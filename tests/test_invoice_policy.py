"""Politique d'import : automatique par defaut, humain seulement en cas de doute.

La regle demandee est stricte dans les deux sens : une facture propre ne
doit PAS demander de confirmation, et une facture douteuse ne doit JAMAIS
etre ecrite sans accord humain.
"""
from decimal import Decimal
from pathlib import Path

import pytest

from app.invoice_pdf import extract_invoice_fields
from app.invoice_policy import (
    ACTION_AUTO,
    ACTION_DUPLICATE,
    ACTION_REVIEW,
    DuplicateState,
    decide_invoice,
    fingerprint,
)

REAL_PDF_TEXT = (
    Path(__file__).parent / "fixtures" / "facture_test_pdf.txt"
).read_text(encoding="utf-8")


def clean():
    return extract_invoice_fields(REAL_PDF_TEXT)


# --- import automatique ---------------------------------------------------

def test_a_readable_complete_coherent_invoice_is_imported_automatically():
    decision = decide_invoice(clean())
    assert decision.action == ACTION_AUTO
    assert decision.reasons == []
    assert decision.is_auto is True


# --- les six cas de validation humaine ------------------------------------

def test_an_unreadable_critical_field_requires_a_human():
    fields = clean()
    fields.montant_ht = None
    fields.missing = ["montant_ht"]
    decision = decide_invoice(fields)
    assert decision.action == ACTION_REVIEW
    assert any("montant HT" in r for r in decision.reasons)


def test_an_incoherent_total_requires_a_human():
    fields = clean()
    fields.montant_ttc = Decimal("4900.00")
    decision = decide_invoice(fields)
    assert decision.action == ACTION_REVIEW
    assert "HT + TVA ne correspond pas au TTC" in decision.reasons


def test_a_missing_supplier_ice_requires_a_human():
    fields = clean()
    fields.ice_fournisseur = None
    decision = decide_invoice(fields)
    assert decision.action == ACTION_REVIEW
    assert any("ICE du fournisseur absent" in r for r in decision.reasons)


def test_an_ambiguous_supplier_requires_a_human():
    decision = decide_invoice(clean(), supplier_ambiguous=True)
    assert decision.action == ACTION_REVIEW
    assert any("fournisseur ambigu" in r for r in decision.reasons)


def test_several_possible_values_require_a_human():
    fields = clean()
    fields.ambigus = ["montant_ttc"]
    decision = decide_invoice(fields)
    assert decision.action == ACTION_REVIEW
    assert any("plusieurs valeurs possibles" in r for r in decision.reasons)


def test_a_credit_note_requires_a_human():
    fields = clean()
    fields.is_avoir = True
    decision = decide_invoice(fields)
    assert decision.action == ACTION_REVIEW
    assert any("avoir" in r for r in decision.reasons)


def test_an_uncertain_duplicate_requires_a_human():
    decision = decide_invoice(clean(), duplicates=DuplicateState(uncertain=True))
    assert decision.action == ACTION_REVIEW
    assert any("doublon possible" in r for r in decision.reasons)


@pytest.mark.parametrize("field_name", ["montant_tva", "taux_tva"])
def test_an_insufficiently_confident_amount_field_requires_a_human(field_name):
    fields = clean()
    setattr(fields, field_name, None)
    decision = decide_invoice(fields)
    assert decision.action == ACTION_REVIEW
    assert decision.reasons


# --- doublon certain ------------------------------------------------------

def test_a_certain_duplicate_is_never_written():
    decision = decide_invoice(
        clean(), duplicates=DuplicateState(certain=True, existing_ref="FA-2026-013")
    )
    assert decision.action == ACTION_DUPLICATE
    assert decision.existing_ref == "FA-2026-013"
    assert decision.is_auto is False


def test_a_certain_duplicate_wins_over_every_other_doubt():
    fields = clean()
    fields.ice_fournisseur = None
    fields.is_avoir = True
    decision = decide_invoice(fields, duplicates=DuplicateState(certain=True))
    assert decision.action == ACTION_DUPLICATE


# --- motifs ---------------------------------------------------------------

def test_the_reason_for_a_wrong_total_is_never_listed_twice():
    fields = clean()
    fields.montant_ttc = Decimal("4900.00")
    fields.anomalies = ["HT + TVA = 4800 mais TTC indique = 4900 (ecart 100)"]
    reasons = decide_invoice(fields).reasons
    assert len([r for r in reasons if "HT + TVA" in r]) == 1


def test_every_doubt_is_reported_not_just_the_first():
    fields = clean()
    fields.ice_fournisseur = None
    fields.is_avoir = True
    reasons = decide_invoice(fields).reasons
    assert any("ICE" in r for r in reasons)
    assert any("avoir" in r for r in reasons)


# --- cle de doublon -------------------------------------------------------

def test_the_fingerprint_is_case_insensitive_on_the_invoice_number():
    assert fingerprint("111", "fac-1") == fingerprint("111", "FAC-1")


def test_no_fingerprint_without_an_ice():
    assert fingerprint("", "FAC-1") == ""
    assert fingerprint(None, "FAC-1") == ""
