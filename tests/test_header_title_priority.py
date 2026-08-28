"""Le titre de l'EN-TETE prime sur une mention plus loin dans le texte.

Cas reel : une facture fournisseur de deux pages dont la seconde precise
"Le bon de livraison porte la meme reference que la facture". La mention
declenchait la regle "BON DE LIVRAISON", plus specifique que la regle
"FACTURE" : la facture sortait de la comptabilite pour aller dans les
documents commerciaux, sans qu'aucune anomalie ne soit levee.
"""
from app.doc_types import (
    BANK_STATEMENT,
    DELIVERY_NOTE,
    PURCHASE_INVOICE,
    QUOTE,
    SALES_INVOICE,
    classify,
)

PAGE1 = "\n".join([
    "FACTURE FOURNISSEUR", "F2026-1102", "Page 1/2",
    "FOURNISSEUR", "MAGHREB TECH SARL",
    "CLIENT", "X BLASTE",
    "Total HT", "3 200.00 MAD", "TOTAL TTC", "3 840.00 MAD",
])
PAGE2 = "\n".join([
    "FACTURE FOURNISSEUR", "F2026-1102", "Page 2/2",
    "Conditions et detail de livraison",
    "Les marchandises ont ete livrees au siege du client. Le bon de livraison"
    " porte la meme reference que la facture.",
])


def test_a_delivery_note_mention_does_not_reclassify_an_invoice():
    doc = classify(PAGE1 + "\n" + PAGE2, company="X BLASTE")
    assert doc.doc_type == PURCHASE_INVOICE
    assert doc.confidence >= 0.95


def test_a_real_delivery_note_is_still_recognised():
    texte = "\n".join([
        "BON DE LIVRAISON", "BL-2026-004", "FOURNISSEUR", "MAGHREB TECH SARL",
        "Marchandises livrees au siege du client.",
    ])
    assert classify(texte, company="X BLASTE").doc_type == DELIVERY_NOTE


def test_a_quote_is_still_recognised():
    texte = "\n".join(["DEVIS", "DV-2026-010", "FOURNISSEUR", "ATLAS PRO SARL",
                       "Ce document ne constitue pas une facture."])
    assert classify(texte, company="X BLASTE").doc_type == QUOTE


def test_a_sales_invoice_header_wins_too():
    texte = "\n".join(["FACTURE CLIENT - VENTE", "V2026-1151", "Page 1/1",
                       "EMETTEUR", "X BLASTE",
                       "Le bon de livraison accompagne la marchandise."])
    assert classify(texte, company="X BLASTE").doc_type == SALES_INVOICE


def test_a_bank_statement_header_wins_too():
    texte = "\n".join(["RELEVE BANCAIRE", "REL-BP-2026-08", "Page 1/1",
                       "BANQUE POPULAIRE", "Titulaire : X BLASTE",
                       "VIR ATLAS PRO - F2026-1101"])
    assert classify(texte, company="X BLASTE").doc_type == BANK_STATEMENT
