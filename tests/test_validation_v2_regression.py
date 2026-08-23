"""Regressions du Pack V2 : cinq documents ecrits automatiquement a tort.

Chaque cas part du TEXTE, passe par la vraie extraction et la vraie
politique. Ce sont les deux etages qui ont failli : la politique savait
bloquer, l'extraction ne levait jamais le drapeau qu'elle attend.
"""
import pytest

from app.doc_extract import extract_document
from app.doc_policy import ACTION_AUTO, ACTION_REVIEW, ACTION_UNKNOWN, decide
from app.doc_types import PAYMENT_RECEIPT, PURCHASE_INVOICE, SUPPLIER_CREDIT_NOTE, UNKNOWN

NL = chr(10)


def document(lignes: list[str]):
    return extract_document([NL.join(lignes)], company="X BLASTE")


def motifs(doc) -> str:
    return " | ".join(decide(doc).reasons)


# --- temoin : une facture propre doit RESTER automatique ------------------

def test_a_clean_invoice_is_still_written_automatically():
    doc = document([
        "FACTURE FOURNISSEUR",
        "Numero de facture : FAC-V2-ACH-001",
        "Date : 2026-08-23",
        "Fournisseur : MAGHREB PRINT SARL",
        "ICE : 003101010000081",
        "Total HT : 5000.00 MAD",
        "TVA 20% : 1000.00 MAD",
        "Total TTC : 6000.00 MAD",
    ])
    decision = decide(doc)
    assert doc.doc_type == PURCHASE_INVOICE
    assert decision.action == ACTION_AUTO, motifs(doc)


# --- 1. plusieurs TTC possibles ------------------------------------------

def test_two_possible_totals_never_go_to_accounting():
    """FAC-V2-AMB-002 : 'Total TTC' et 'Net a payer' se contredisent."""
    doc = document([
        "FACTURE FOURNISSEUR",
        "Numero de facture : FAC-V2-AMB-002",
        "Date : 2026-08-23",
        "Fournisseur : OMEGA FOURNITURES SARL",
        "ICE : 003711223000091",
        "Total HT : 2000.00 MAD",
        "TVA 20% : 400.00 MAD",
        "Total TTC : 2400.00 MAD",
        "Net a payer : 2450.00 MAD",
    ])
    assert "montant_ttc" in doc.ambigus
    decision = decide(doc)
    assert decision.action == ACTION_REVIEW
    assert any("plusieurs valeurs possibles" in r for r in decision.reasons)


# --- 2. devise etrangere sans taux ---------------------------------------

def test_a_foreign_currency_without_rate_is_never_converted():
    """FAC-V2-USD-001 : 1 000 USD ne devient jamais 1 000 MAD."""
    doc = document([
        "PURCHASE INVOICE",
        "Numero de facture : FAC-V2-USD-001",
        "Date : 2026-08-23",
        "Fournisseur : GLOBAL TECH SUPPLIES LLC",
        "ICE : 003844556000071",
        "Total HT : 1000.00 USD",
        "Total TTC : 1000.00 USD",
    ])
    assert doc.devise == "USD"
    assert "USD" in doc.devises_detectees
    decision = decide(doc)
    assert decision.action == ACTION_REVIEW
    assert any("taux de change" in r for r in decision.reasons)


def test_a_foreign_amount_beside_a_mad_total_also_blocks():
    doc = document([
        "FACTURE FOURNISSEUR",
        "Numero de facture : FAC-V2-USD-002",
        "Date : 2026-08-23",
        "Fournisseur : GLOBAL TECH SUPPLIES LLC",
        "ICE : 003844556000071",
        "Prix unitaire : 100.00 USD",
        "Total HT : 1000.00 MAD",
        "TVA 20% : 200.00 MAD",
        "Total TTC : 1200.00 MAD",
    ])
    decision = decide(doc)
    assert decision.action == ACTION_REVIEW
    assert any("taux de change" in r for r in decision.reasons), motifs(doc)


# --- 3. TTC absent --------------------------------------------------------

def test_a_zero_total_is_treated_as_a_missing_total():
    """FAC-V2-INC-001 : un TTC a 0,00 n'est pas un total, c'est une absence."""
    doc = document([
        "FACTURE FOURNISSEUR",
        "Numero de facture : FAC-V2-INC-001",
        "Date : 2026-08-23",
        "Fournisseur : BETA SERVICES SARL",
        "ICE : 003922334000051",
        "Total HT : 1800.00 MAD",
        "TVA 20% : 360.00 MAD",
        "Total TTC : 0.00 MAD",
    ])
    decision = decide(doc)
    assert decision.action == ACTION_REVIEW
    assert any("TTC absent ou nul" in r for r in decision.reasons), motifs(doc)


# --- 4. avoir sans facture d'origine -------------------------------------

def test_a_credit_note_without_a_real_origin_invoice_waits():
    """AV-V2-INC-001 : 'NON COMMUNIQUEE' n'est pas une facture d'origine."""
    doc = document([
        "AVOIR FOURNISSEUR",
        "Numero d'avoir : AV-V2-INC-001",
        "Date : 2026-08-23",
        "Fournisseur : TECH OFFICE SARL",
        "ICE : 003202020000092",
        "Facture d'origine : NON COMMUNIQUEE",
        "Total HT : 300.00 MAD",
        "TVA 20% : 60.00 MAD",
        "Total TTC : 360.00 MAD",
    ])
    assert doc.doc_type == SUPPLIER_CREDIT_NOTE
    assert doc.facture_liee is None
    assert doc.facture_liee_brute
    decision = decide(doc)
    assert decision.action == ACTION_REVIEW
    assert any("facture d'origine" in r for r in decision.reasons)


def test_a_credit_note_with_a_real_origin_invoice_still_passes():
    doc = document([
        "AVOIR FOURNISSEUR",
        "Numero d'avoir : AV-V2-ACH-001",
        "Date : 2026-08-23",
        "Fournisseur : MAGHREB PRINT SARL",
        "ICE : 003101010000081",
        "Facture d'origine : FAC-V2-ACH-001",
        "Total HT : 500.00 MAD",
        "TVA 20% : 100.00 MAD",
        "Total TTC : 600.00 MAD",
    ])
    assert doc.facture_liee == "FAC-V2-ACH-001"
    assert decide(doc).action == ACTION_AUTO, motifs(doc)


# --- 5. contrat ----------------------------------------------------------

def test_a_contract_is_never_a_payment_receipt():
    """CTR-V2-2026-009 : un contrat est classe, jamais comptabilise."""
    doc = document([
        "CONTRAT DE PRESTATION DE SERVICES",
        "Reference : CTR-V2-2026-009",
        "Date : 2026-08-23",
        "Prestataire : X BLASTE",
        "Modalites de paiement : virement a 30 jours",
        "Montant mensuel : 2500.00 MAD",
    ])
    assert doc.doc_type == UNKNOWN
    assert doc.doc_type != PAYMENT_RECEIPT
    decision = decide(doc)
    assert decision.action == ACTION_UNKNOWN
    assert not decision.writes_accounting


# --- 6. raison sociale inexploitable -------------------------------------

def test_an_unusable_party_name_blocks_the_write():
    """Le fournisseur 'EN' ne doit jamais creer de fiche ni de charge."""
    doc = document([
        "FACTURE FOURNISSEUR",
        "Numero de facture : FAC-V2-EN-001",
        "Date : 2026-08-23",
        "Fournisseur : EN",
        "Total HT : 1000.00 MAD",
        "TVA 20% : 200.00 MAD",
        "Total TTC : 1200.00 MAD",
    ])
    decision = decide(doc)
    assert decision.action == ACTION_REVIEW
    assert any("raison sociale" in r for r in decision.reasons), motifs(doc)


# --- 7. ecart TTC deja couvert, verifie de bout en bout -------------------

def test_a_ttc_discrepancy_still_waits():
    """FAC-V2-AMB-001 : HT + TVA = 3600 mais TTC annonce 3900."""
    doc = document([
        "FACTURE FOURNISSEUR",
        "Numero de facture : FAC-V2-AMB-001",
        "Date : 2026-08-23",
        "Fournisseur : DATA NORTH V2 SARL",
        "ICE : 003611223000080",
        "Total HT : 3000.00 MAD",
        "TVA 20% : 600.00 MAD",
        "Total TTC : 3900.00 MAD",
    ])
    decision = decide(doc)
    assert decision.action == ACTION_REVIEW
    assert any("TTC" in r for r in decision.reasons)
