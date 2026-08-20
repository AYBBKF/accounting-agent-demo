"""Generation de factures et de releves bancaires FICTIFS pour la demo.

Aucune donnee reelle : fournisseurs, montants et libelles sont
clairement marques comme fictifs. Generation deterministe (seed
explicite) pour rester testable.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

FAKE_SUPPLIERS = [
    "Fournitures Atlas SARL (DEMO)",
    "Papeterie Zellige (DEMO)",
    "Transport Sindibad (DEMO)",
    "Cyber Cafe Medina (DEMO)",
    "Imprimerie Argan (DEMO)",
]

FAKE_CATEGORIES = ["fournitures", "transport", "services", "informatique", "impression"]


@dataclass(frozen=True)
class DemoInvoice:
    fournisseur: str
    numero: str
    date_facture: date
    montant_ht: Decimal
    taux_tva: Decimal


@dataclass(frozen=True)
class DemoBankLine:
    date_operation: date
    libelle: str
    montant: Decimal


def generate_demo_invoices(
    count: int = 5, seed: int = 42, allowed_rates: list[Decimal] | None = None
) -> list[DemoInvoice]:
    rng = random.Random(seed)
    rates = allowed_rates or [Decimal("20"), Decimal("10"), Decimal("7"), Decimal("0")]
    invoices: list[DemoInvoice] = []
    base_day = date.today()
    for i in range(count):
        supplier = rng.choice(FAKE_SUPPLIERS)
        montant_ht = Decimal(rng.randrange(500, 20000)) / Decimal("100")
        taux = rng.choice(rates)
        d = base_day - timedelta(days=rng.randint(0, 30))
        invoices.append(
            DemoInvoice(
                fournisseur=supplier,
                numero=f"DEMO-{2026}-{1000 + i}",
                date_facture=d,
                montant_ht=montant_ht,
                taux_tva=taux,
            )
        )
    return invoices


def generate_demo_bank_statement(
    invoices: list[DemoInvoice], seed: int = 7, noise_lines: int = 3
) -> list[DemoBankLine]:
    """Cree un releve bancaire fictif : la majorite des factures y apparaissent
    (montant TTC, date decalee de 0 a 3 jours), plus quelques lignes de bruit
    non liees a une facture (pour tester le rapprochement)."""
    from app.vat import simulate_vat

    rng = random.Random(seed)
    lines: list[DemoBankLine] = []
    for inv in invoices:
        if rng.random() < 0.85:
            result = simulate_vat(inv.montant_ht, inv.taux_tva)
            offset = timedelta(days=rng.randint(0, 3))
            lines.append(
                DemoBankLine(
                    date_operation=inv.date_facture + offset,
                    libelle=f"VIR {inv.fournisseur[:20]} {inv.numero}",
                    montant=-result.montant_ttc,
                )
            )
    for i in range(noise_lines):
        lines.append(
            DemoBankLine(
                date_operation=date.today() - timedelta(days=rng.randint(0, 30)),
                libelle=f"OPERATION DIVERSE DEMO {i}",
                montant=Decimal(rng.randrange(-5000, 5000)) / Decimal("100"),
            )
        )
    return lines
