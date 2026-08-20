from datetime import date
from decimal import Decimal

from app.demo_data import DemoBankLine, DemoInvoice
from app.reconciliation import reconcile_invoices
from app.vat import simulate_vat


def test_reconcile_finds_matching_line():
    inv = DemoInvoice(
        fournisseur="Test (DEMO)",
        numero="DEMO-1",
        date_facture=date(2026, 1, 10),
        montant_ht=Decimal("100.00"),
        taux_tva=Decimal("20"),
    )
    ttc = simulate_vat(inv.montant_ht, inv.taux_tva).montant_ttc
    line = DemoBankLine(date_operation=date(2026, 1, 11), libelle="VIR Test", montant=-ttc)

    results = reconcile_invoices([inv], [line])
    assert results[0].status == "rapprochee"
    assert results[0].bank_line == line


def test_reconcile_reports_no_match_without_inventing_one():
    inv = DemoInvoice(
        fournisseur="Test (DEMO)",
        numero="DEMO-2",
        date_facture=date(2026, 1, 10),
        montant_ht=Decimal("100.00"),
        taux_tva=Decimal("20"),
    )
    unrelated_line = DemoBankLine(date_operation=date(2026, 1, 10), libelle="Autre", montant=Decimal("-5.00"))

    results = reconcile_invoices([inv], [unrelated_line])
    assert results[0].status == "non_rapprochee"
    assert results[0].bank_line is None


def test_reconcile_respects_date_window():
    inv = DemoInvoice(
        fournisseur="Test (DEMO)",
        numero="DEMO-3",
        date_facture=date(2026, 1, 1),
        montant_ht=Decimal("50.00"),
        taux_tva=Decimal("20"),
    )
    ttc = simulate_vat(inv.montant_ht, inv.taux_tva).montant_ttc
    # Meme montant mais bien au-dela de la fenetre de rapprochement.
    far_line = DemoBankLine(date_operation=date(2026, 3, 1), libelle="VIR", montant=-ttc)

    results = reconcile_invoices([inv], [far_line], window_days=5)
    assert results[0].status == "non_rapprochee"
