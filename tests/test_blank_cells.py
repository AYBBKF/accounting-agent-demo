"""Une valeur inconnue s'ecrit VIDE, jamais `null`.

L'API Sheets refuse `null` dans un tableau de valeurs et rejette la ligne
ENTIERE. Comme une ecriture n'est jamais rejouee, la facture ne partait pas
du tout en comptabilite - silencieusement. Constate en reel sur une facture
dont le taux de TVA etait illisible : la ligne comptable n'a jamais ete
ecrite, alors que HT, TVA et TTC etaient parfaitement lus.
"""
from datetime import date
from decimal import Decimal

from app.invoice_sheet import build_row_plan, to_number


def test_an_unknown_value_becomes_an_empty_cell():
    assert to_number(None) == ""
    assert to_number(Decimal("20")) == 20.0


def plan(taux):
    return build_row_plan(
        tab="05_FACTURES_ACHATS", row_index=2, stable_id="FA-2026-001",
        supplier_id="FRS-001", supplier_name="ATLAS PRO SARL",
        numero="F2026-1101", description="Import email - F2026-1101",
        date_facture=date(2026, 8, 15), date_echeance=date(2026, 9, 15),
        montant_ht=Decimal("5000"), taux_tva=taux,
        montant_tva=Decimal("1000"), montant_ttc=Decimal("6000"),
    )


def test_a_row_never_carries_a_null():
    """Aucune cellule ne doit valoir None, quel que soit le taux."""
    for taux in (None, Decimal("20")):
        for value in plan(taux).values_a_j + plan(taux).values_n_p:
            assert value is not None, (taux, value)


def test_an_unreadable_rate_leaves_only_its_own_cell_empty():
    values = plan(None).values_a_j
    assert values[7] == ""          # Taux TVA inconnu
    assert values[6] == 5000.0      # HT toujours ecrit
    assert values[8] == 1000.0      # TVA toujours ecrite
    assert values[9] == 6000.0      # TTC toujours ecrit


def test_a_readable_rate_is_still_written():
    assert plan(Decimal("20")).values_a_j[7] == 20.0
