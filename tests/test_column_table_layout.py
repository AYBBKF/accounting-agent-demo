"""Factures dont l'identite est presentee en TABLEAU DE COLONNES.

Mise en page tres repandue : une ligne par cellule d'en-tete
("NUMERO", "DATE", "ECHEANCE"), puis une ligne par cellule de valeur.
Le libelle et sa valeur ne sont pas voisins. Avant correctif, la date lue
etait "ECHEANCE" (donc aucune), et toute facture de ce type partait en
quarantaine "date du document illisible ou absent".
"""
from datetime import date
from decimal import Decimal

import pytest

from app.doc_extract import extract_document
from app.doc_policy import ACTION_AUTO, ACTION_REVIEW, DecisionContext, decide

TODAY = DecisionContext(today=date(2026, 8, 28))


def facture(numero="F2026-1101", date_doc="15/08/2026", ttc="6 000.00 MAD",
            tva_ligne="TVA 2E+1 %", tva_montant="1 000.00 MAD", ice=True):
    ice_ligne = "ICE fournisseur : 001122334455667" if ice else "IF 24567189 - RC 518204"
    return "\n".join([
        "FACTURE FOURNISSEUR", numero, "Page 1/1",
        "FOURNISSEUR", "ATLAS PRO SARL", "12 rue Ibn Sina, Casablanca, Maroc",
        ice_ligne,
        "CLIENT", "X BLASTE", "25 boulevard Zerktouni, Casablanca, Maroc",
        "ICE client : 003456789000052",
        "NUMERO", "DATE", "ECHEANCE",
        numero, date_doc, "15/09/2026",
        "Designation", "Qte", "P.U. HT", "Montant HT",
        "Fournitures de bureau", "1", "5 000.00 MAD", "5 000.00 MAD",
        "Total HT", "5 000.00 MAD",
        tva_ligne, tva_montant,
        "TOTAL TTC", ttc,
        f"Reference de paiement : {numero}",
    ])


def test_the_date_comes_from_its_own_column_not_the_next_header():
    doc = extract_document([facture()], company="X BLASTE")
    assert doc.date_document == date(2026, 8, 15)
    assert doc.date_echeance == date(2026, 9, 15)


def test_the_number_comes_from_the_numero_column():
    doc = extract_document([facture()], company="X BLASTE")
    assert doc.numero == "F2026-1101"


def test_a_payment_reference_line_never_becomes_the_invoice_number():
    """'Reference de paiement : F2026-1101' donnait le numero "de"."""
    doc = extract_document([facture()], company="X BLASTE")
    assert doc.numero != "de"


def test_a_clean_column_table_invoice_is_booked():
    doc = extract_document([facture()], company="X BLASTE")
    assert doc.missing == []
    assert doc.anomalies == []
    assert decide(doc, TODAY).action == ACTION_AUTO


def test_an_unreadable_vat_rate_still_yields_the_vat_amount():
    """Le taux illisible ne doit pas faire disparaitre le MONTANT de TVA."""
    doc = extract_document([facture()], company="X BLASTE")
    assert doc.montant_tva is not None
    assert doc.montant_tva.value == Decimal("1000.00")


def test_the_rate_is_left_unknown_rather_than_invented():
    doc = extract_document([facture()], company="X BLASTE")
    assert doc.taux_tva is None


def test_incoherent_totals_are_still_caught_when_the_rate_is_unreadable():
    """Le controle HT + TVA = TTC doit survivre a un taux illisible.

    Avant correctif le montant de TVA n'etait pas lu, le controle etait
    silencieusement saute, et une facture incoherente partait en
    comptabilite.
    """
    doc = extract_document([facture(numero="F2026-1103", ttc="6 450.00 MAD")],
                           company="X BLASTE")
    assert any("TTC" in a for a in doc.anomalies)
    assert decide(doc, TODAY).action == ACTION_REVIEW


def test_a_readable_rate_is_still_read():
    doc = extract_document([facture(tva_ligne="TVA 21 %", tva_montant="262.50 MAD",
                                    ttc="5 262.50 MAD")], company="X BLASTE")
    assert doc.taux_tva == Decimal("21")


def test_a_supplier_invoice_without_ice_stays_in_quarantine():
    doc = extract_document([facture(numero="F2026-1104", ice=False)], company="X BLASTE")
    assert decide(doc, TODAY).action == ACTION_REVIEW


def test_a_party_name_starting_with_a_role_word_still_yields_its_ice():
    """'CLIENT NOVA SARL' coupait la recherche de l'ICE du bloc client."""
    pages = ["\n".join([
        "FACTURE CLIENT - VENTE", "V2026-1151", "Page 1/1",
        "EMETTEUR", "X BLASTE", "ICE emetteur : 003456789000052",
        "CLIENT", "CLIENT NOVA SARL", "91 boulevard Anfa, Casablanca, Maroc",
        "ICE client : 005566778899001",
        "NUMERO", "DATE", "ECHEANCE",
        "V2026-1151", "20/08/2026", "20/09/2026",
        "Total HT", "8 000.00 MAD", "TVA 2E+1 %", "1 600.00 MAD",
        "TOTAL TTC", "9 600.00 MAD",
    ])]
    doc = extract_document(pages, company="X BLASTE")
    assert doc.destinataire_ice == "005566778899001"


def test_a_label_followed_by_its_value_is_unchanged():
    """Non-regression : la mise en page 'Libelle' puis 'valeur' reste lue."""
    pages = ["\n".join([
        "AVOIR FOURNISSEUR",
        "Fournisseur : TECH OFFICE SARL", "ICE : 003202020000092",
        "Client : X BLASTE",
        "Numero d'avoir", "AV-2026-9", "Date", "23/08/2026",
        "Facture d'origine", "NON PRECISEE",
        "Total HT", "-300.00 MAD", "TVA", "-60.00 MAD",
        "Total TTC", "-360.00 MAD",
    ])]
    doc = extract_document(pages, company="X BLASTE")
    assert doc.date_document == date(2026, 8, 23)
    assert doc.numero == "AV-2026-9"
    # "NON PRECISEE" n'est pas une reference, et surtout pas un montant.
    assert doc.facture_liee is None
