"""Decision : import automatique, validation humaine, ou doublon.

Module PUR : aucun I/O, aucun appel reseau, aucun modele de langage. La
decision est donc reproductible et entierement testable.

Import automatique (aucun bouton) uniquement si TOUT est vrai :
  type certain, champs obligatoires presents, montants lisibles,
  HT + TVA = TTC dans la tolerance, devise connue, tiers identifie sans
  ambiguite, pas de doublon, aucun conflit avec l'existant.

Validation humaine dans les seuls cas de doute reels enumeres par le
client. Un doublon CERTAIN n'est jamais ecrit : on informe, sans bouton.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.doc_extract import CURRENCIES, ExtractedDocument
from app.doc_types import (
    ACCOUNTING_TYPES,
    BANK_STATEMENT,
    CLIENT_CREDIT_NOTE,
    EXPORT_INVOICE,
    IMPORT_INVOICE,
    PAYMENT_RECEIPT,
    PENALTY_NOTICE,
    PURCHASE_INVOICE,
    SALES_INVOICE,
    SUPPLIER_CREDIT_NOTE,
    UNKNOWN,
)

ACTION_AUTO = "auto"
ACTION_REVIEW = "review"
ACTION_DUPLICATE = "duplicate"
ACTION_UNKNOWN = "unknown"          # a deposer dans Drive "A verifier"

# Tolerance explicite sur HT + TVA = TTC : un centime d'arrondi ne doit pas
# bloquer un import, un ecart reel doit le bloquer.
DEFAULT_TOLERANCE = Decimal("0.01")

# En dessous, le type n'est pas considere comme certain.
MIN_TYPE_CONFIDENCE = 0.90

# Devise de tenue de la comptabilite. Toute autre devise exige un taux de
# change exploitable avant la moindre ecriture.
BOOKKEEPING_CURRENCY = "MAD"

# Types pour lesquels l'ICE du tiers est indispensable : ce sont ceux qui
# creent ou completent une fiche tiers et une ecriture comptable.
NEEDS_PARTY_ID = frozenset({
    PURCHASE_INVOICE, SALES_INVOICE, SUPPLIER_CREDIT_NOTE, CLIENT_CREDIT_NOTE,
})


@dataclass
class DuplicateState:
    certain: bool = False
    uncertain: bool = False
    existing_ref: str = ""


@dataclass
class DecisionContext:
    """Ce que le monde exterieur sait deja, au moment de decider."""

    duplicates: DuplicateState = field(default_factory=DuplicateState)
    party_ambiguous: bool = False
    party_reason: str = ""
    # Nombre de factures candidates pour un recu de paiement.
    receipt_matches: int = 0
    # Taux de change disponible pour une facture en devise etrangere.
    exchange_rate: Decimal | None = None
    tolerance: Decimal = DEFAULT_TOLERANCE


@dataclass
class Decision:
    action: str
    reasons: list[str] = field(default_factory=list)
    existing_ref: str = ""

    @property
    def is_auto(self) -> bool:
        return self.action == ACTION_AUTO

    @property
    def writes_accounting(self) -> bool:
        return self.action == ACTION_AUTO


_FIELD_LABELS = {
    "numero": "numero du document",
    "date_document": "date du document",
    "date_echeance": "date d'echeance",
    "emetteur": "emetteur",
    "destinataire": "destinataire",
    "montant_ht": "montant HT",
    "montant_tva": "montant TVA",
    "montant_ttc": "montant TTC",
    "montant_paye": "montant paye",
}


def _label(name: str) -> str:
    return _FIELD_LABELS.get(name, name)


def decide(doc: ExtractedDocument, context: DecisionContext | None = None) -> Decision:
    """Decide du sort d'un document extrait. Ne modifie rien."""
    ctx = context or DecisionContext()

    # 1. Doublon certain : jamais d'ecriture, simple information.
    if ctx.duplicates.certain:
        return Decision(
            action=ACTION_DUPLICATE,
            reasons=["document deja importe (meme tiers et meme numero)"],
            existing_ref=ctx.duplicates.existing_ref,
        )

    # 2. Type inconnu : direction la zone "A verifier", pas la comptabilite.
    if doc.doc_type == UNKNOWN:
        return Decision(
            action=ACTION_UNKNOWN,
            reasons=doc.classification.reasons or ["type de document non reconnu"],
        )

    reasons: list[str] = []

    if doc.classification.confidence < MIN_TYPE_CONFIDENCE:
        reasons.append(
            f"type de document incertain ({doc.classification.confidence:.0%})"
        )

    # 3. Champs obligatoires et montants lisibles.
    for name in doc.missing:
        reasons.append(f"{_label(name)} illisible ou absent")

    # 4. Coherence HT + TVA = TTC, avec tolerance explicite.
    if doc.montant_ht and doc.montant_tva and doc.montant_ttc:
        ecart = abs(doc.montant_ht.value + doc.montant_tva.value - doc.montant_ttc.value)
        if ecart > ctx.tolerance:
            reasons.append(
                f"HT + TVA ne correspond pas au TTC (ecart {ecart} > tolerance {ctx.tolerance})"
            )

    # 5. Plusieurs montants possibles pour un meme champ.
    for name in doc.ambigus:
        reasons.append(f"plusieurs valeurs possibles pour {_label(name)}")

    # 6. Devise.
    if doc.amounts:
        if not doc.devise:
            reasons.append("devise absente ou non reconnue")
        elif doc.devise not in CURRENCIES:
            reasons.append(f"devise inconnue : {doc.devise}")
        elif doc.devise != BOOKKEEPING_CURRENCY and doc.doc_type in ACCOUNTING_TYPES:
            if ctx.exchange_rate is None:
                reasons.append(
                    f"facture en {doc.devise} sans taux de change exploitable"
                )

    # 7. Tiers.
    if ctx.party_ambiguous:
        reasons.append(ctx.party_reason or "tiers ambigu (plusieurs correspondances)")
    if doc.doc_type in NEEDS_PARTY_ID:
        needed_ice = (
            doc.emetteur_ice if doc.doc_type in (PURCHASE_INVOICE, SUPPLIER_CREDIT_NOTE)
            else doc.destinataire_ice
        )
        if not needed_ice:
            reasons.append("ICE du tiers absent du document")

    # 8. Avoirs : l'imputation sur la facture d'origine engage la
    #    comptabilite dans les deux sens. Jamais sans accord humain.
    if doc.doc_type in (SUPPLIER_CREDIT_NOTE, CLIENT_CREDIT_NOTE):
        reasons.append("avoir : imputation a confirmer avant comptabilisation")

    # 9. Recu de paiement : ne jamais solder une facture sur une simple
    #    ressemblance de montant.
    if doc.doc_type == PAYMENT_RECEIPT:
        if ctx.receipt_matches == 0:
            reasons.append("aucune facture ne correspond a ce recu")
        elif ctx.receipt_matches > 1:
            reasons.append(
                f"{ctx.receipt_matches} factures peuvent correspondre a ce recu"
            )

    # 10. Penalite : sans echeance certaine, pas de rappel automatique.
    if doc.doc_type == PENALTY_NOTICE and doc.date_echeance is None:
        reasons.append("echeance de la penalite ambigue")

    # 11. Releve bancaire : toute incoherence de solde bloque l'import.
    if doc.doc_type == BANK_STATEMENT and not doc.bank_lines:
        reasons.append("aucune operation exploitable dans le releve")

    # 12. Autres anomalies deterministes remontees a l'extraction.
    for anomaly in doc.anomalies:
        if "HT + TVA" in anomaly:
            continue  # deja couvert en 4
        reasons.append(anomaly)

    # 13. Doublon incertain.
    if ctx.duplicates.uncertain:
        reasons.append("doublon possible mais non certain")

    if reasons:
        return Decision(
            action=ACTION_REVIEW, reasons=reasons, existing_ref=ctx.duplicates.existing_ref
        )
    return Decision(action=ACTION_AUTO)


def fingerprint(party_id: str | None, numero: str | None) -> str:
    """Cle de doublon metier : (identifiant du tiers + numero du document).

    Vide si l'un des deux manque : sans identifiant fiable, aucun doublon ne
    peut etre affirme avec certitude, et le document part en validation.
    """
    party = (party_id or "").strip()
    number = (numero or "").strip().upper()
    if not party or not number:
        return ""
    return f"{party}|{number}"
