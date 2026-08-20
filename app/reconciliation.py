"""Rapprochement bancaire simplifie pour la demo.

Associe chaque facture (TTC) a une ligne de releve bancaire fictive par
proximite de montant (tolerance configurable) et de date (fenetre en
jours configurable). Jamais de correspondance inventee : si rien ne
correspond, le statut est explicitement "non_rapprochee".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.demo_data import DemoBankLine, DemoInvoice
from app.vat import simulate_vat


@dataclass(frozen=True)
class ReconciliationResult:
    invoice: DemoInvoice
    bank_line: DemoBankLine | None
    status: str  # "rapprochee" | "non_rapprochee"
    detail: str


def reconcile_invoices(
    invoices: list[DemoInvoice],
    bank_lines: list[DemoBankLine],
    amount_tolerance: Decimal = Decimal("0.01"),
    window_days: int = 5,
) -> list[ReconciliationResult]:
    remaining = list(bank_lines)
    results: list[ReconciliationResult] = []

    for inv in invoices:
        vat = simulate_vat(inv.montant_ht, inv.taux_tva)
        expected_amount = -vat.montant_ttc
        match: DemoBankLine | None = None
        for line in remaining:
            if abs(line.montant - expected_amount) > amount_tolerance:
                continue
            delta_days = abs((line.date_operation - inv.date_facture).days)
            if delta_days > window_days:
                continue
            match = line
            break

        if match is not None:
            remaining.remove(match)
            results.append(
                ReconciliationResult(
                    invoice=inv,
                    bank_line=match,
                    status="rapprochee",
                    detail=f"Correspondance sur montant {match.montant} et date {match.date_operation.isoformat()}",
                )
            )
        else:
            results.append(
                ReconciliationResult(
                    invoice=inv,
                    bank_line=None,
                    status="non_rapprochee",
                    detail="Aucune ligne bancaire correspondante trouvee (montant/date)",
                )
            )
    return results
