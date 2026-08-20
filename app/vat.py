"""Simulation TVA : HT -> TVA -> TTC, taux configurables (jamais code en dur).

Toutes les valeurs monetaires sont des Decimal. Aucune valeur n'est
inventee : si le taux n'est pas dans la liste configuree, on leve une
erreur explicite plutot que de deviner.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")


class UnknownVatRateError(ValueError):
    pass


@dataclass(frozen=True)
class VatSimulationResult:
    montant_ht: Decimal
    taux_tva: Decimal
    montant_tva: Decimal
    montant_ttc: Decimal


def _round(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def simulate_vat(
    montant_ht: Decimal, taux_tva: Decimal, allowed_rates: list[Decimal] | None = None
) -> VatSimulationResult:
    if allowed_rates is not None and taux_tva not in allowed_rates:
        raise UnknownVatRateError(
            f"Taux de TVA {taux_tva} non present dans la configuration {allowed_rates}"
        )
    montant_tva = _round(montant_ht * taux_tva / Decimal("100"))
    montant_ttc = _round(montant_ht + montant_tva)
    return VatSimulationResult(
        montant_ht=_round(montant_ht),
        taux_tva=taux_tva,
        montant_tva=montant_tva,
        montant_ttc=montant_ttc,
    )


def totals_are_coherent(
    montant_ht: Decimal, montant_tva: Decimal, montant_ttc: Decimal, tolerance: Decimal = Decimal("0.02")
) -> bool:
    """Verifie HT + TVA == TTC a une tolerance d'arrondi pres."""
    return abs((montant_ht + montant_tva) - montant_ttc) <= tolerance
