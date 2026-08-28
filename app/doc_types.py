"""Classification DETERMINISTE du type d'un document.

Aucun modele de langage n'intervient : le type est decide par des marqueurs
cherches en MOTS ENTIERS dans le texte normalise, evalues dans un ordre de
specificite decroissante. Ce choix est volontaire - une erreur de type ici
envoie un devis dans le chiffre d'affaires ou une penalite dans les achats.

Deux pieges reels, constates sur les documents du client :

  - le mot "DEVISE" (present sur toute facture en devise etrangere) ne doit
    jamais declencher "DEVIS". D'ou la recherche en mots entiers ;
  - un devis, un bon de commande et un recu contiennent tous le mot
    "facture" ("ne constitue pas une facture", "Facture reglee"). Le type
    ne peut donc pas etre decide par la simple presence de ce mot : les
    regles specifiques passent AVANT la regle generique.

La couche texte des PDF coupe les titres sur plusieurs lignes
("FACTURE\\nFOURNISSEUR", "DEVIS - NON CO\\nMPTABILISABLE"). La recherche se
fait donc sur le texte normalise complet, espaces reduits, jamais ligne par
ligne.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# --- les 13 types demandes ------------------------------------------------
PURCHASE_INVOICE = "facture_achat"
SALES_INVOICE = "facture_vente"
SUPPLIER_CREDIT_NOTE = "avoir_fournisseur"
CLIENT_CREDIT_NOTE = "avoir_client"
QUOTE = "devis"
PURCHASE_ORDER = "bon_commande"
DELIVERY_NOTE = "bon_livraison"
BANK_STATEMENT = "releve_bancaire"
PAYMENT_RECEIPT = "recu_paiement"
PENALTY_NOTICE = "avis_penalite"
IMPORT_INVOICE = "facture_import"
EXPORT_INVOICE = "facture_export"
UNKNOWN = "inconnu"

ALL_TYPES = (
    PURCHASE_INVOICE, SALES_INVOICE, SUPPLIER_CREDIT_NOTE, CLIENT_CREDIT_NOTE,
    QUOTE, PURCHASE_ORDER, DELIVERY_NOTE, BANK_STATEMENT, PAYMENT_RECEIPT,
    PENALTY_NOTICE, IMPORT_INVOICE, EXPORT_INVOICE, UNKNOWN,
)

# Types qui produisent une ecriture comptable. Les autres sont classes et
# journalises, mais ne creent ni chiffre d'affaires, ni TVA, ni charge.
ACCOUNTING_TYPES = frozenset({
    PURCHASE_INVOICE, SALES_INVOICE, SUPPLIER_CREDIT_NOTE, CLIENT_CREDIT_NOTE,
    BANK_STATEMENT, PAYMENT_RECEIPT, PENALTY_NOTICE, IMPORT_INVOICE,
    EXPORT_INVOICE,
})

# Types dont les montants sont, par nature, negatifs.
SIGNED_NEGATIVE_TYPES = frozenset({SUPPLIER_CREDIT_NOTE, CLIENT_CREDIT_NOTE})

LABELS = {
    PURCHASE_INVOICE: "Facture d'achat",
    SALES_INVOICE: "Facture de vente",
    SUPPLIER_CREDIT_NOTE: "Avoir fournisseur",
    CLIENT_CREDIT_NOTE: "Avoir client",
    QUOTE: "Devis",
    PURCHASE_ORDER: "Bon de commande",
    DELIVERY_NOTE: "Bon de livraison",
    BANK_STATEMENT: "Releve bancaire",
    PAYMENT_RECEIPT: "Recu de paiement",
    PENALTY_NOTICE: "Avis de penalite",
    IMPORT_INVOICE: "Facture d'importation",
    EXPORT_INVOICE: "Facture d'exportation",
    UNKNOWN: "Document inconnu",
}


# Marqueurs de contrat. Un contrat cite des montants, des echeances et des
# modalites de paiement : sans cette liste, il etait absorbe par la regle
# du recu de paiement.
CONTRACT_MARKERS = (
    "CONTRAT", "CONTRAT DE PRESTATION", "CONTRAT DE MAINTENANCE",
    "CONTRAT DE SERVICE", "CONTRAT CADRE", "CONVENTION", "AVENANT",
    "SERVICE AGREEMENT", "AGREEMENT", "CONTRACT", "MANDAT", "BAIL",
    "CONDITIONS GENERALES",
)


def strip_accents(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", value or "")
        if unicodedata.category(c) != "Mn"
    )


def normalize(value: str) -> str:
    """Majuscules, sans accents, espaces reduits a un seul.

    Les titres coupes par la mise en page ("FACTURE\\nFOURNISSEUR") se
    retrouvent ainsi sur une seule ligne logique.
    """
    return re.sub(r"\s+", " ", strip_accents(value)).strip().upper()


def has_phrase(text: str, phrase: str) -> bool:
    """Cherche une expression en MOTS ENTIERS dans le texte normalise."""
    return bool(re.search(rf"\b{re.escape(normalize(phrase))}\b", text))


def has_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(has_phrase(text, p) for p in phrases)


@dataclass
class Classification:
    doc_type: str
    confidence: float          # 0.0 a 1.0
    matched: str               # marqueur qui a decide, pour tracabilite
    reasons: list[str]

    @property
    def is_accounting(self) -> bool:
        return self.doc_type in ACCOUNTING_TYPES

    @property
    def label(self) -> str:
        return LABELS.get(self.doc_type, self.doc_type)


# Marqueurs de "notre" societe : sert a decider achat vs vente sur une
# facture generique. Surchargeable (le nom de la societe est configurable).
def _party_role(text: str, company: str) -> str | None:
    """Notre societe est-elle l'emetteur ou le destinataire ?

    Retourne "emetteur", "destinataire" ou None si indecidable. Les libelles
    reels sont "Fournisseur : X", "Emetteur : X", "Client : X", "Acheteur :
    X", "Importer : X", "Beneficiaire : X".
    """
    me = normalize(company)
    if not me:
        return None
    issuer = re.search(rf"\b(?:FOURNISSEUR|EMETTEUR|EXPORTER|VENDEUR)\s*:?\s*{re.escape(me)}\b", text)
    if issuer:
        return "emetteur"
    receiver = re.search(
        rf"\b(?:CLIENT|ACHETEUR|IMPORTER|IMPORTATEUR|DESTINATAIRE|BENEFICIAIRE)\s*:?\s*{re.escape(me)}\b",
        text,
    )
    if receiver:
        return "destinataire"
    return None


# Titres qui designent SANS AMBIGUITE la nature du document. Cherches
# uniquement dans l'EN-TETE (premieres lignes de la premiere page), ils
# priment sur toute mention ulterieure.
_EXPLICIT_TITLES: tuple[tuple[str, str, float], ...] = (
    ("RELEVE BANCAIRE", BANK_STATEMENT, 0.98),
    ("AVOIR FOURNISSEUR", SUPPLIER_CREDIT_NOTE, 0.96),
    ("AVOIR CLIENT", CLIENT_CREDIT_NOTE, 0.96),
    ("FACTURE FOURNISSEUR", PURCHASE_INVOICE, 0.95),
    ("FACTURE D ACHAT", PURCHASE_INVOICE, 0.95),
    ("PURCHASE INVOICE", PURCHASE_INVOICE, 0.95),
    ("FACTURE DE VENTE", SALES_INVOICE, 0.95),
    ("FACTURE CLIENT", SALES_INVOICE, 0.95),
    ("SALES INVOICE", SALES_INVOICE, 0.95),
)

_TITLE_LINES = 5


def header_of(text: str) -> str:
    """En-tete du document : ses premieres lignes non vides, normalisees."""
    lignes = [l for l in (text or "").splitlines() if l.strip()][:_TITLE_LINES]
    return normalize("\n".join(lignes))


def classify(text: str, *, company: str = "X BLASTE") -> Classification:
    """Determine le type d'un document a partir de son CONTENU.

    L'ordre des regles va du plus specifique au plus generique : un document
    qui declare explicitement "BON DE COMMANDE" n'est jamais evalue par la
    regle generique "FACTURE", meme s'il contient le mot facture dans une
    mention de bas de page.
    """
    t = normalize(text)
    if not t:
        return Classification(UNKNOWN, 0.0, "", ["document vide ou illisible"])

    role = _party_role(t, company)

    # 0. Titre explicite en EN-TETE. Une facture de deux pages dont la
    #    seconde precise "le bon de livraison porte la meme reference" etait
    #    classee BON DE LIVRAISON : la simple mention, plus loin dans le
    #    texte, l'emportait sur le titre de la page 1. Elle sortait donc de
    #    la comptabilite pour aller dans les documents commerciaux.
    entete = header_of(text)
    for phrase, kind, score in _EXPLICIT_TITLES:
        if has_phrase(entete, phrase):
            return Classification(kind, score, phrase, [])

    # 1. Releve bancaire : structure totalement differente d'une facture.
    if has_any(t, ("RELEVE BANCAIRE", "RELEVE DE COMPTE", "BANK STATEMENT",
                   "EXTRAIT DE COMPTE")):
        return Classification(BANK_STATEMENT, 0.98, "RELEVE BANCAIRE", [])

    # 2. Avis de penalite / taxe : ne doit jamais devenir une facture
    #    fournisseur ordinaire.
    if has_any(t, ("AVIS DE PENALITE", "PENALITE A PAYER", "AVIS D IMPOSITION",
                   "AVIS DE TAXE", "MISE EN DEMEURE", "AVIS A PAYER")):
        return Classification(PENALTY_NOTICE, 0.97, "AVIS DE PENALITE", [])

    # 3. Contrats et conventions. Ils parlent de paiement, d'echeances et de
    #    montants, et tombaient donc dans la regle "recu de paiement" : un
    #    contrat de prestation etait classe "Recu de paiement" a 96 % de
    #    confiance. Un contrat n'est PAS une piece comptable : il est classe,
    #    archive, et n'ecrit rien.
    if has_any(t, CONTRACT_MARKERS):
        return Classification(UNKNOWN, 0.30, "CONTRAT", [
            "contrat ou convention : aucune ecriture comptable, classement seul"
        ])

    # 4. Recu / preuve de paiement : contient "Facture reglee", d'ou la
    #    priorite avant toute regle "FACTURE".
    if has_any(t, ("RECU DE PAIEMENT", "PREUVE DE PAIEMENT", "ACCUSE DE PAIEMENT",
                   "PAYMENT RECEIPT", "QUITTANCE")):
        return Classification(PAYMENT_RECEIPT, 0.96, "RECU DE PAIEMENT", [])

    # 5. Bons : documents logistiques, jamais comptabilises.
    if has_phrase(t, "BON DE COMMANDE") or has_phrase(t, "PURCHASE ORDER"):
        return Classification(PURCHASE_ORDER, 0.96, "BON DE COMMANDE", [])
    if has_phrase(t, "BON DE LIVRAISON") or has_phrase(t, "DELIVERY NOTE"):
        return Classification(DELIVERY_NOTE, 0.96, "BON DE LIVRAISON", [])

    # 6. Devis. "DEVISE" ne matche pas : la recherche est en mots entiers.
    if has_any(t, ("DEVIS", "PROFORMA", "QUOTATION", "PRO FORMA")):
        return Classification(QUOTE, 0.95, "DEVIS", [])

    # 7. Avoirs, avant les factures : un avoir contient le mot facture
    #    (facture d'origine).
    if has_any(t, ("AVOIR FOURNISSEUR", "NOTE DE CREDIT FOURNISSEUR")):
        return Classification(SUPPLIER_CREDIT_NOTE, 0.96, "AVOIR FOURNISSEUR", [])
    if has_any(t, ("AVOIR CLIENT", "NOTE DE CREDIT CLIENT")):
        return Classification(CLIENT_CREDIT_NOTE, 0.96, "AVOIR CLIENT", [])
    if has_any(t, ("AVOIR", "NOTE DE CREDIT", "CREDIT NOTE")):
        # Avoir sans qualificatif : le sens depend de qui emet le document.
        if role == "destinataire":
            return Classification(SUPPLIER_CREDIT_NOTE, 0.80, "AVOIR", [
                "avoir non qualifie, sens deduit de la position des parties"
            ])
        if role == "emetteur":
            return Classification(CLIENT_CREDIT_NOTE, 0.80, "AVOIR", [
                "avoir non qualifie, sens deduit de la position des parties"
            ])
        return Classification(SUPPLIER_CREDIT_NOTE, 0.55, "AVOIR", [
            "avoir non qualifie et parties non identifiees"
        ])

    # 8. Commerce international, avant les factures nationales.
    if has_any(t, ("COMMERCIAL INVOICE", "FACTURE D IMPORTATION",
                   "FACTURE IMPORT")):
        return Classification(IMPORT_INVOICE, 0.95, "COMMERCIAL INVOICE", [])
    if has_any(t, ("FACTURE COMMERCIALE EXPORT", "FACTURE D EXPORTATION",
                   "FACTURE EXPORT", "EXPORT INVOICE")):
        return Classification(EXPORT_INVOICE, 0.95, "FACTURE EXPORT", [])

    # 9. Factures explicitement qualifiees.
    if has_any(t, ("FACTURE DE VENTE", "FACTURE CLIENT", "SALES INVOICE")):
        return Classification(SALES_INVOICE, 0.95, "FACTURE DE VENTE", [])
    if has_any(t, ("FACTURE FOURNISSEUR", "FACTURE D ACHAT", "PURCHASE INVOICE")):
        return Classification(PURCHASE_INVOICE, 0.95, "FACTURE FOURNISSEUR", [])

    # 10. Facture generique : le sens vient de la position de notre societe.
    if has_any(t, ("FACTURE", "INVOICE", "FATURA", "RECHNUNG")):
        if role == "destinataire":
            return Classification(PURCHASE_INVOICE, 0.85, "FACTURE", [
                "type deduit : notre societe est le client"
            ])
        if role == "emetteur":
            return Classification(SALES_INVOICE, 0.85, "FACTURE", [
                "type deduit : notre societe est l'emetteur"
            ])
        return Classification(UNKNOWN, 0.40, "FACTURE", [
            "facture detectee mais impossible de dire achat ou vente"
        ])

    return Classification(UNKNOWN, 0.0, "", ["aucun marqueur de type reconnu"])
