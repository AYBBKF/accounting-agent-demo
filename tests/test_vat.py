from decimal import Decimal

import pytest

from app.vat import UnknownVatRateError, simulate_vat, totals_are_coherent


def test_simulate_vat_basic():
    result = simulate_vat(Decimal("100.00"), Decimal("20"))
    assert result.montant_tva == Decimal("20.00")
    assert result.montant_ttc == Decimal("120.00")


def test_simulate_vat_rejects_unknown_rate():
    with pytest.raises(UnknownVatRateError):
        simulate_vat(Decimal("100.00"), Decimal("15"), allowed_rates=[Decimal("20"), Decimal("10")])


def test_totals_are_coherent_true():
    assert totals_are_coherent(Decimal("100.00"), Decimal("20.00"), Decimal("120.00")) is True


def test_totals_are_coherent_false_on_wrong_total():
    # Facture avec un total incoherent (HT+TVA != TTC) doit etre detectee.
    assert totals_are_coherent(Decimal("100.00"), Decimal("20.00"), Decimal("999.00")) is False


def test_all_values_are_decimal_never_float():
    result = simulate_vat(Decimal("33.33"), Decimal("7"))
    assert isinstance(result.montant_ht, Decimal)
    assert isinstance(result.montant_tva, Decimal)
    assert isinstance(result.montant_ttc, Decimal)
