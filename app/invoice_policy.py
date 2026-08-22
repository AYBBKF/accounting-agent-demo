"""Decision d'import : automatique, validation humaine, ou doublon.

Regle metier demandee par le client :

  - facture lisible + champs obligatoires presents + confiance elevee +
    HT + TVA = TTC  ->  import automatique, sans confirmation ;
  - validation humaine UNIQUEMENT si un montant ou un champ critique est
    illisible, si HT + TVA ne correspond pas au TTC, si le fournisseur est
    ambigu ou si l'ICE manque, si plusieurs valeurs sont possibles, si le
    document ressemble a un avoir ou a un doublon incertain, ou si la
    confiance d'un champ critique est insuffisante ;
  - un doublon CERTAIN n'est jamais ecrit : on informe seulement.

Ce module est volontairement pur (aucun I/O, aucun appel reseau, aucun
modele de langage) : la decision est donc reproductible et testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.invoice_pdf import ExtractedInvoice

# Actions possibles.
ACTION_AUTO = "auto"
ACTION_REVIEW = "review"
ACTION_DUPLICATE = "duplicate"

# Champs sans lesquels une ligne comptable ne peut pas etre ecrite
# correctement (au-dela des REQUIRED_FIELDS de l'extraction).
CRITICAL_AMOUNT_FIELDS = ("montant_ht", "montant_tva", "montant_ttc", "taux_tva")

_FIELD_LABELS = {
    "numero": "numero de facture",
    "date_facture": "date de facture",
    "fournisseur": "fournisseur",
    "montant_ht": "montant HT",
    "montant_tva": "montant TVA",
    "montant_ttc": "montant TTC",
    "taux_tva": "taux de TVA",
}


def _label(name: str) -> str:
    return _FIELD_LABELS.get(name, name)


@dataclass
class DuplicateState:
    """Ce que l'on sait deja de cette facture, cote base et cote classeur."""

    # (ICE fournisseur + numero de facture) deja importe : doublon certain.
    certain: bool = False
    # Numero deja present mais fournisseur non confirme (ICE inconnu, ou
    # meme numero chez un autre fournisseur) : doute, donc humain.
    uncertain: bool = False
    # Ligne deja existante, pour le message d'information.
    existing_ref: str = ""


@dataclass
class Decision:
    action: str
    reasons: list[str] = field(default_factory=list)
    existing_ref: str = ""

    @property
    def is_auto(self) -> bool:
        return self.action == ACTION_AUTO


def decide_invoice(
    fields: ExtractedInvoice,
    *,
    duplicates: DuplicateState | None = None,
    supplier_ambiguous: bool = False,
) -> Decision:
    """Decide du sort d'une facture extraite. Ne modifie rien."""
    duplicates = duplicates or DuplicateState()

    # 1. Doublon certain : on n'ecrit jamais, on informe.
    if duplicates.certain:
        return Decision(
            action=ACTION_DUPLICATE,
            reasons=["facture deja importee (meme ICE fournisseur et meme numero)"],
            existing_ref=duplicates.existing_ref,
        )

    reasons: list[str] = []

    # 2. Champ critique illisible ou absent.
    for name in fields.missing:
        reasons.append(f"{_label(name)} illisible ou absent")
    for name in CRITICAL_AMOUNT_FIELDS:
        if getattr(fields, name, None) is None and name not in fields.missing:
            reasons.append(f"{_label(name)} illisible ou absent")

    # 3. Coherence HT + TVA = TTC (recalculee en Decimal a l'extraction).
    if (
        fields.montant_ht is not None
        and fields.montant_tva is not None
        and fields.montant_ttc is not None
        and fields.montant_ht + fields.montant_tva != fields.montant_ttc
    ):
        reasons.append("HT + TVA ne correspond pas au TTC")

    # 4. Fournisseur ambigu ou ICE manquant.
    if not fields.ice_fournisseur:
        reasons.append("ICE du fournisseur absent du document")
    if supplier_ambiguous:
        reasons.append("fournisseur ambigu (plusieurs correspondances possibles)")

    # 5. Plusieurs valeurs possibles pour un champ critique.
    for name in fields.ambigus:
        reasons.append(f"plusieurs valeurs possibles pour {_label(name)}")

    # 6. Avoir, ou doublon incertain.
    if fields.is_avoir:
        reasons.append("le document semble etre un avoir")
    if duplicates.uncertain:
        reasons.append("doublon possible mais non certain")

    # 7. Autres anomalies detectees a l'extraction (dates incoherentes...).
    for anomaly in fields.anomalies:
        if "HT + TVA" in anomaly:
            continue  # deja couvert en 3, on ne duplique pas le motif
        reasons.append(anomaly)

    if reasons:
        return Decision(
            action=ACTION_REVIEW, reasons=reasons, existing_ref=duplicates.existing_ref
        )
    return Decision(action=ACTION_AUTO)


def fingerprint(ice_fournisseur: str | None, numero: str | None) -> str:
    """Cle de doublon : (ICE fournisseur + numero de facture).

    Retourne "" si l'un des deux manque : sans ICE, aucun doublon ne peut
    etre declare CERTAIN, et la facture part en validation humaine.
    """
    ice = (ice_fournisseur or "").strip()
    numero = (numero or "").strip().upper()
    if not ice or not numero:
        return ""
    return f"{ice}|{numero}"
