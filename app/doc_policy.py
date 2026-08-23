"""Decision : import automatique, validation humaine, ou doublon.

Module PUR : aucun I/O, aucun appel reseau, aucun modele de langage. La
decision est donc reproductible et entierement testable.

Regle de conduite : un document LISIBLE et COHERENT part en comptabilite
sans rien demander. La validation humaine est reservee aux situations ou
aucune decision comptable fiable n'est possible :

  - contradiction entre HT, TVA et TTC ;
  - plusieurs montants possibles pour un meme champ ;
  - type de document incertain ;
  - plusieurs tiers existants peuvent correspondre ;
  - doublon probable mais non certain ;
  - recu pouvant solder plusieurs factures ;
  - devise etrangere sans taux de change exploitable ;
  - avoir dont la facture d'origine est inconnue ou multiple.

Tout le reste est un AVERTISSEMENT : signale dans le journal d'import et
dans la notification, sans jamais retenir l'ecriture. L'ICE absent en est
le cas type. Un doublon CERTAIN n'est jamais ecrit : on informe, sans
bouton.
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

# Un nom de tiers doit etre exploitable. "EN" - deux lettres captees dans un
# document anglophone - a suffi a creer une fiche fournisseur et a ecrire une
# charge. Sans nom credible, aucune ecriture.
MIN_PARTY_NAME_LENGTH = 3
PARTY_NAME_STOPWORDS = frozenset({
    "EN", "FR", "DE", "ES", "IT", "TVA", "VAT", "HT", "TTC", "NON", "OUI",
    "N/A", "NA", "SA", "SARL", "SAS", "LTD", "GMBH", "INC", "CO", "TOTAL",
    "FACTURE", "INVOICE", "CLIENT", "FOURNISSEUR", "SUPPLIER", "CUSTOMER",
})


def usable_party_name(name: str | None) -> bool:
    """Le nom identifie-t-il reellement un tiers ?"""
    candidate = (name or "").strip().strip(":;,.").strip()
    if len(candidate) < MIN_PARTY_NAME_LENGTH:
        return False
    return candidate.upper() not in PARTY_NAME_STOPWORDS


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
    # Nombre de factures candidates pour l'imputation d'un avoir.
    credit_note_targets: int = 1
    # Taux de change disponible pour une facture en devise etrangere.
    exchange_rate: Decimal | None = None
    tolerance: Decimal = DEFAULT_TOLERANCE


@dataclass
class Decision:
    action: str
    reasons: list[str] = field(default_factory=list)
    existing_ref: str = ""
    # Ce qui merite d'etre SIGNALE sans empecher l'ecriture. Un champ
    # secondaire absent n'a jamais justifie de reveiller le client : il
    # apparait dans le journal d'import et dans la notification, et
    # l'ecriture comptable a lieu quand meme.
    warnings: list[str] = field(default_factory=list)

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
    warnings: list[str] = []

    if doc.classification.confidence < MIN_TYPE_CONFIDENCE:
        reasons.append(
            f"type de document incertain ({doc.classification.confidence:.0%})"
        )

    # 3. Champs obligatoires et montants lisibles.
    for name in doc.missing:
        reasons.append(f"{_label(name)} illisible ou absent")

    # 3bis. Un TTC lu comme ZERO n'est pas un TTC : c'est un total absent que
    #       l'extraction a transforme en nombre. Il ne doit jamais devenir une
    #       ecriture a 0,00, avec un ecart egal a tout le montant de la facture.
    if doc.doc_type in ACCOUNTING_TYPES and doc.doc_type != BANK_STATEMENT:
        if doc.montant_ttc is not None and doc.montant_ttc.value == 0:
            reasons.append("montant TTC absent ou nul")

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

    # 6. Devise. La regle est une DETECTION POSITIVE : tant que le document
    #    n'affirme pas sa devise, aucune ecriture. Se contenter d'une valeur
    #    vide revenait a supposer MAD et a comptabiliser 1 000 USD comme
    #    1 000 MAD.
    if doc.amounts:
        etrangeres = [
            c for c in (doc.devises_detectees or [])
            if c in CURRENCIES and c != BOOKKEEPING_CURRENCY
        ]
        if not doc.devise:
            reasons.append("devise absente ou non reconnue")
        elif doc.devise not in CURRENCIES:
            reasons.append(f"devise inconnue : {doc.devise}")
        elif doc.doc_type in ACCOUNTING_TYPES:
            if doc.devise != BOOKKEEPING_CURRENCY and ctx.exchange_rate is None:
                reasons.append(
                    f"facture en {doc.devise} sans taux de change exploitable"
                )
            elif etrangeres and ctx.exchange_rate is None:
                # Le total retenu est en MAD, mais le document porte aussi des
                # montants en devise etrangere : convertir ou ignorer serait
                # une decision comptable, pas une lecture.
                reasons.append(
                    "montants en {} dans le document sans taux de change "
                    "exploitable".format(", ".join(etrangeres))
                )

    # 7. Tiers. SEULE l'ambiguite bloque : plusieurs fiches existantes
    #    peuvent correspondre au meme nom, et choisir a la place du client
    #    reviendrait a imputer une charge au mauvais fournisseur.
    #
    #    L'ICE absent, lui, ne bloque plus. Une facture dont les montants
    #    sont coherents et dont le fournisseur est lisible reste parfaitement
    #    comptabilisable : on cree une fiche provisoire, on laisse l'ICE a
    #    completer, et on le SIGNALE. Bloquer la-dessus transformait une
    #    comptabilite automatique en file d'attente de confirmations.
    if ctx.party_ambiguous:
        reasons.append(ctx.party_reason or "tiers ambigu (plusieurs correspondances)")
    if doc.doc_type in NEEDS_PARTY_ID:
        needed_ice = (
            doc.emetteur_ice if doc.doc_type in (PURCHASE_INVOICE, SUPPLIER_CREDIT_NOTE)
            else doc.destinataire_ice
        )
        party_name = (
            doc.emetteur if doc.doc_type in (PURCHASE_INVOICE, SUPPLIER_CREDIT_NOTE)
            else doc.destinataire
        )
        if not usable_party_name(party_name):
            # Un nom de deux lettres, un mot-outil ou un libelle de colonne
            # n'est pas une raison sociale : sans tiers credible, l'ecriture
            # imputerait une charge a n'importe qui.
            reasons.append(
                f"raison sociale du tiers inexploitable : {party_name!r}"
            )
        elif not needed_ice:
            if not (party_name or "").strip():
                # Ni ICE ni raison sociale : plus rien n'identifie le tiers.
                reasons.append("tiers non identifiable (ni ICE ni raison sociale)")
            else:
                warnings.append(
                    f"ICE absent du document : fiche tiers '{party_name}' a completer"
                )

    # 8. Avoirs : ce qui engage la comptabilite dans les deux sens, c'est
    #    l'imputation sur une facture d'origine. Quand cette facture est
    #    citee et que les montants sont coherents, l'ecriture est certaine
    #    et n'a pas besoin d'accord humain ; c'est l'incertitude sur
    #    l'origine ou sur l'effet comptable qui exige une decision.
    if doc.doc_type in (SUPPLIER_CREDIT_NOTE, CLIENT_CREDIT_NOTE):
        if not (doc.facture_liee or "").strip():
            brute = (getattr(doc, "facture_liee_brute", "") or "").strip()
            if brute:
                reasons.append(
                    f"avoir sans facture d'origine identifiable "
                    f"(champ lu : {brute!r})"
                )
            else:
                reasons.append("avoir sans facture d'origine identifiable")
        elif ctx.credit_note_targets > 1:
            reasons.append(
                f"{ctx.credit_note_targets} factures peuvent correspondre a cet avoir"
            )

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
            action=ACTION_REVIEW, reasons=reasons,
            existing_ref=ctx.duplicates.existing_ref, warnings=warnings,
        )
    return Decision(action=ACTION_AUTO, warnings=warnings)


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
