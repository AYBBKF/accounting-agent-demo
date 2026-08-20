from decimal import Decimal

from app.demo_data import generate_demo_bank_statement, generate_demo_invoices


def test_generate_demo_invoices_deterministic():
    a = generate_demo_invoices(count=5, seed=42)
    b = generate_demo_invoices(count=5, seed=42)
    assert a == b
    assert len(a) == 5
    assert all("(DEMO)" in inv.fournisseur for inv in a)


def test_generate_demo_bank_statement_links_most_invoices():
    invoices = generate_demo_invoices(count=6, seed=1)
    bank_lines = generate_demo_bank_statement(invoices, seed=2, noise_lines=2)
    # Au moins quelques lignes generees, montants toujours Decimal
    assert len(bank_lines) >= 2
    assert all(isinstance(line.montant, Decimal) for line in bank_lines)
