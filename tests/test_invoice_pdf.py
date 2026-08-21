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
