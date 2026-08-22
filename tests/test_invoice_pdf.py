"""Tests de l'extraction deterministe des champs de facture.

La fixture tests/fixtures/facture_test_pdf.txt est le texte REEL extrait du
PDF Facture_test_X_BLASTE_FAC-TEST-2026-001.pdf recu par email (empreinte
SHA-256 identique au PDF d'origine).
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.invoice_pdf import (
    ExtractedInvoice,
    InvoicePdfError,
    extract_invoice_fields,
    parse_fr_date,
    parse_money,
)

REAL_PDF_TEXT = (
    Path(__file__).parent / "fixtures" / "facture_test_pdf.txt"
).read_text(encoding="utf-8")


@pytest.fixture
def extracted() -> ExtractedInvoice:
    return extract_invoice_fields(REAL_PDF_TEXT)


# --- les valeurs attendues, telles que fournies par le client -------------

@pytest.mark.parametrize(
    "champ,attendu",
    [
        ("numero", "FAC-TEST-2026-001"),
        ("date_facture", date(2026, 8, 21)),
        ("date_echeance", date(2026, 9, 20)),
        ("fournisseur", "ATLAS BUREAU SARL"),
        ("client", "X BLASTE"),
        ("montant_ht", Decimal("4000.00")),
        ("taux_tva", Decimal("20")),
        ("montant_tva", Decimal("800.00")),
        ("montant_ttc", Decimal("4800.00")),
        ("devise", "MAD"),
        ("statut", "NON PAYEE"),
        ("mode_paiement", "Virement bancaire"),
    ],
)
def test_every_expected_field_is_extracted(extracted, champ, attendu):
    assert getattr(extracted, champ) == attendu


def test_real_invoice_has_no_missing_field_and_no_anomaly(extracted):
    assert extracted.missing == []
    assert extracted.anomalies == []
    assert extracted.is_complete is True
    assert extracted.needs_human_review is False


def test_totals_are_consistent(extracted):
    assert extracted.montant_ht + extracted.montant_tva == extracted.montant_ttc


# --- garde-fous : jamais de valeur inventee ------------------------------

def test_missing_fields_are_reported_not_guessed():
    result = extract_invoice_fields("FACTURE\nDivers\nMerci de votre confiance.")
    assert result.numero is None
    assert result.montant_ttc is None
    assert set(result.missing) >= {"numero", "montant_ht", "montant_ttc"}
    assert result.needs_human_review is True


def test_empty_pdf_text_raises_instead_of_returning_zeros():
    with pytest.raises(InvoicePdfError):
        extract_invoice_fields("   ")


def test_ttc_mismatch_is_flagged_as_anomaly():
    text = REAL_PDF_TEXT.replace(" 4 800.00 MAD", " 5 000.00 MAD")
    result = extract_invoice_fields(text)
    assert result.montant_ttc == Decimal("5000.00")
    assert any("TTC" in a for a in result.anomalies)
    assert result.needs_human_review is True


def test_due_date_before_invoice_date_is_flagged():
    text = REAL_PDF_TEXT.replace("20/09/2026", "20/07/2026")
    result = extract_invoice_fields(text)
    assert any("echeance" in a for a in result.anomalies)


# --- parsing bas niveau ---------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4 000.00 MAD", Decimal("4000.00")),
        ("1 500,00 MAD", Decimal("1500.00")),
        ("800.00", Decimal("800.00")),
        ("2 500.00 MAD", Decimal("2500.00")),
    ],
)
def test_parse_money(raw, expected):
    parsed = parse_money(raw)
    assert parsed is not None and parsed[0] == expected


@pytest.mark.parametrize("raw", ["", "   ", "Non payee", None])
def test_parse_money_refuses_to_guess(raw):
    assert parse_money(raw) is None


def test_parse_money_keeps_the_currency():
    assert parse_money("4 000.00 MAD")[1] == "MAD"


@pytest.mark.parametrize(
    "raw,expected",
    [("21/08/2026", date(2026, 8, 21)), ("2026-08-21", date(2026, 8, 21)),
     ("21-08-2026", date(2026, 8, 21))],
)
def test_parse_fr_date(raw, expected):
    assert parse_fr_date(raw) == expected


@pytest.mark.parametrize("raw", ["", "pas une date", "32/13/2026"])
def test_parse_fr_date_refuses_invalid_input(raw):
    assert parse_fr_date(raw) is None


# --- ICE, lignes de detail, ambiguites ------------------------------------
# Ajoutes pour l'import automatique : sans ICE fiable, aucun doublon ne peut
# etre affirme et aucun fournisseur ne peut etre rattache.

def test_the_supplier_ice_is_taken_from_the_issuer_block_not_the_client_one():
    fields = extract_invoice_fields(REAL_PDF_TEXT)
    assert fields.ice_fournisseur == "002345678000043"
    assert fields.ice_client == "003456789000051"


def test_no_ice_is_invented_when_the_document_has_none():
    text = REAL_PDF_TEXT.replace("ICE: 002345678000043   IF: 18765432", "IF: 18765432")
    fields = extract_invoice_fields(text)
    assert fields.ice_fournisseur is None
    assert fields.ice_client == "003456789000051"


def test_detail_lines_are_extracted_with_exact_decimals():
    from decimal import Decimal

    lignes = extract_invoice_fields(REAL_PDF_TEXT).lignes
    assert len(lignes) == 2
    assert lignes[0].description == "Ramettes papier A4 premium"
    assert lignes[0].quantite == Decimal("2")
    assert lignes[0].prix_unitaire_ht == Decimal("750.00")
    assert lignes[0].total_ht == Decimal("1500.00")
    assert lignes[1].total_ht == Decimal("2500.00")
    # Le total des lignes correspond au total HT annonce : aucune ligne perdue.
    assert sum(l.total_ht for l in lignes) == Decimal("4000.00")


def test_a_product_label_containing_a_digit_is_never_read_as_a_total():
    # "Ramettes papier A4 premium" ne doit jamais devenir un montant de 4.
    fields = extract_invoice_fields(REAL_PDF_TEXT)
    from decimal import Decimal

    assert fields.montant_ht == Decimal("4000.00")
    assert "montant_ht" not in fields.ambigus


def test_two_different_totals_are_reported_as_ambiguous():
    text = REAL_PDF_TEXT.replace(
        " TOTAL TTC\n 4 800.00 MAD", " TOTAL TTC\n 4 800.00 MAD\n TOTAL TTC\n 5 000.00 MAD"
    )
    assert "montant_ttc" in extract_invoice_fields(text).ambigus


def test_a_clean_invoice_has_no_ambiguity_and_is_not_a_credit_note():
    fields = extract_invoice_fields(REAL_PDF_TEXT)
    assert fields.ambigus == []
    assert fields.is_avoir is False
    assert fields.needs_human_review is False


def test_a_credit_note_is_flagged():
    text = REAL_PDF_TEXT.replace("FACTURE\n", "FACTURE D AVOIR\n")
    assert extract_invoice_fields(text).is_avoir is True


# --- reconnaissance du type de document -----------------------------------
# La requete Gmail ne filtre plus sur l'objet : c'est le contenu du PDF qui
# decide. Un document non comptable ne doit jamais atteindre le classeur.

def test_a_real_invoice_is_recognised():
    assert extract_invoice_fields(REAL_PDF_TEXT).is_invoice is True


QUOTE = (
    "DEVIS\nN° DEV-2026-010\nDATE DE FACTURE\n21/08/2026\n"
    " Total HT\n 1 000.00 MAD\n TOTAL TTC\n 1 200.00 MAD\n"
)


@pytest.mark.parametrize(
    "titre", ["DEVIS", "BON DE COMMANDE", "BON DE LIVRAISON", "CONTRAT", "PROFORMA"]
)
def test_a_non_accounting_document_is_rejected(titre):
    text = QUOTE.replace("DEVIS", titre, 1)
    assert extract_invoice_fields(text).is_invoice is False


def test_the_word_devise_never_makes_an_invoice_look_like_a_quote():
    # "DEVISE / MAD" figure sur toute facture marocaine : la recherche doit
    # se faire en mots entiers, sinon aucune facture ne passerait.
    assert "DEVISE" in REAL_PDF_TEXT.upper()
    assert extract_invoice_fields(REAL_PDF_TEXT).is_invoice is True


def test_a_document_without_any_amount_is_not_an_invoice():
    assert extract_invoice_fields("FACTURE\nN° FAC-1\nMerci de votre confiance.").is_invoice is False


def test_an_unrelated_document_is_not_an_invoice():
    assert extract_invoice_fields("Compte rendu de reunion\nPoints abordes\nfin").is_invoice is False


def test_a_credit_note_is_still_treated_as_an_accounting_document():
    text = REAL_PDF_TEXT.replace("FACTURE\n", "AVOIR\n", 1)
    fields = extract_invoice_fields(text)
    assert fields.is_invoice is True
    assert fields.is_avoir is True
