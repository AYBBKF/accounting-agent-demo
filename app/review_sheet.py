"""Onglet `21_A_VERIFIER` : la zone de quarantaine comptable.

Module PUR : aucun I/O, aucun reseau. Il ne fait que construire les
valeurs ecrites dans l'onglet et le texte des explications.

Regle fondatrice de cette architecture : un document douteux n'entre
JAMAIS dans la comptabilite. Il n'est plus mis en attente derriere un
bouton Telegram - il est ECRIT, visible, dans un onglet dedie, en rouge,
avec le motif exact de l'anomalie. Le comptable le voit, le comprend, et
decide lui-meme.

Consequence directe, et c'est tout l'interet : les montants de cet onglet
sont ecrits en TEXTE. Ils ne peuvent donc pas etre additionnes, ni entrer
dans un total, ni dans le Dashboard, ni dans la TVA - meme si une formule
pointait un jour sur cette plage par erreur. La garantie ne repose pas sur
la discipline de celui qui ecrira la prochaine formule, mais sur le type
de la donnee elle-meme.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

TAB_REVIEW = "21_A_VERIFIER"

# Rouge clair : lisible, imprimable, et distinct du bleu des lignes
# reellement comptabilisees.
REVIEW_ROW_COLOR = "#F4CCCC"

# Statuts de la colonne L. Le bot n'ecrit jamais autre chose que le
# premier : c'est le comptable qui cloture une ligne.
STATUS_TODO = "A traiter"
STATUS_DONE = "Traite"

REVIEW_HEADERS = [
    "ID document",
    "Detecte le",
    "Type detecte",
    "Numero du document",
    "Tiers lu",
    "Devise lue",
    "Montants lus (texte, hors comptabilite)",
    "Anomalie",
    "Detail de l'anomalie",
    "Lien Drive",
    "Message Gmail",
    "Statut",
]

# Colonnes 1-indexees, pour que l'appelant n'ait pas a compter.
COL_ID = "A"
COL_AMOUNTS = "G"
COL_ANOMALY = "H"
COL_DETAIL = "I"
COL_STATUS = "L"
LAST_COL = "L"

# Longueur du prefixe de cle utilisee comme identifiant visible. Meme
# longueur que celle deja employee dans 14_IMPORTS_LOG, pour qu'une ligne
# de journal et une ligne de quarantaine se rapprochent a l'oeil.
KEY_LENGTH = 12

# Une infobulle Google Sheets est plafonnee : au-dela, elle est tronquee
# par l'interface et devient inutile. Le detail complet reste, lui, dans
# la colonne I qui n'a pas cette limite.
TOOLTIP_MAX = 250


@dataclass
class ReviewEntry:
    """Ce qu'on sait d'un document ecarte de la comptabilite."""

    doc_key: str
    detected_at: str
    type_label: str
    numero: str = ""
    tiers: str = ""
    devise: str = ""
    montant_ht: Decimal | None = None
    montant_tva: Decimal | None = None
    montant_ttc: Decimal | None = None
    reasons: list[str] = field(default_factory=list)
    drive_link: str = ""
    gmail_message_id: str = ""
    filename: str = ""

    @property
    def short_key(self) -> str:
        return (self.doc_key or "")[:KEY_LENGTH]


def _amount(label: str, value: Decimal | None, devise: str) -> str:
    if value is None:
        return f"{label} non lu"
    unit = (devise or "").strip()
    return f"{label} {value}{(' ' + unit) if unit else ''}"


def format_amounts(entry: ReviewEntry) -> str:
    """Montants LUS, en texte, jamais en nombre.

    Le suffixe est explicite : quiconque lit cette cellule doit comprendre
    qu'elle ne participe a aucun total.
    """
    parts = [
        _amount("HT", entry.montant_ht, entry.devise),
        _amount("TVA", entry.montant_tva, entry.devise),
        _amount("TTC", entry.montant_ttc, entry.devise),
    ]
    return " | ".join(parts) + "  [lu, non comptabilise]"


def summarize(reasons: list[str]) -> str:
    """Resume court affiche dans la colonne Anomalie."""
    if not reasons:
        return "anomalie non precisee"
    first = reasons[0].strip()
    if len(reasons) == 1:
        return first
    return f"{first} (+{len(reasons) - 1} autre(s))"


def build_detail(entry: ReviewEntry) -> str:
    """Explication COMPLETE, lisible sans ouvrir le PDF.

    On enonce d'abord ce qui bloque, puis ce que le bot a lu, puis ou
    retrouver la piece. L'ordre compte : le comptable veut savoir pourquoi
    avant de savoir quoi.
    """
    lines = ["Ce document n'a PAS ete comptabilise. Motif(s) :"]
    lines += [f"- {reason}" for reason in (entry.reasons or ["anomalie non precisee"])]
    lines.append("")
    lines.append("Valeurs lues dans le document :")
    lines.append(f"- Type    : {entry.type_label or 'non determine'}")
    lines.append(f"- Numero  : {entry.numero or 'non lu'}")
    lines.append(f"- Tiers   : {entry.tiers or 'non lu'}")
    lines.append(f"- Devise  : {entry.devise or 'non lue'}")
    lines.append(f"- {format_amounts(entry)}")
    lines.append("")
    lines.append(
        "Aucun de ces montants n'entre dans les totaux, le Dashboard ou la TVA."
    )
    if entry.filename:
        lines.append(f"Fichier : {entry.filename}")
    if entry.drive_link:
        lines.append(f"Piece archivee : {entry.drive_link}")
    return "\n".join(lines)


def build_tooltip(entry: ReviewEntry) -> str:
    """Texte de l'infobulle posee sur la cellule Anomalie.

    Google Sheets tronque au-dela d'une certaine longueur : on coupe
    NOUS-MEMES, proprement, plutot que de laisser l'interface couper au
    milieu d'un mot et rendre le motif incomprehensible.
    """
    detail = build_detail(entry)
    if len(detail) <= TOOLTIP_MAX:
        return detail
    coupe = detail[:TOOLTIP_MAX].rsplit(" ", 1)[0].rstrip()
    return f"{coupe}... (detail complet en colonne {COL_DETAIL})"


def build_review_row(entry: ReviewEntry) -> list[str]:
    """La ligne ecrite dans `21_A_VERIFIER`.

    TOUTES les cellules sont des chaines : c'est la garantie structurelle
    qu'aucun montant douteux ne devienne un nombre sommable.
    """
    return [
        entry.short_key,
        entry.detected_at,
        entry.type_label or "non determine",
        entry.numero or "",
        entry.tiers or "",
        entry.devise or "",
        format_amounts(entry),
        summarize(entry.reasons),
        build_detail(entry),
        entry.drive_link or "",
        entry.gmail_message_id or "",
        STATUS_TODO,
    ]


def find_row(existing_ids: list[str], doc_key: str) -> int:
    """Ligne deja occupee par ce document, 0 si absent.

    C'est le garde-fou anti-doublon de l'onglet : un document reexamine a
    chaque cycle Gmail doit REECRIRE sa ligne, jamais en ajouter une.
    `existing_ids` est la colonne A lue a partir de la ligne 2.
    """
    wanted = (doc_key or "")[:KEY_LENGTH]
    if not wanted:
        return 0
    for offset, value in enumerate(existing_ids):
        if str(value or "").strip() == wanted:
            return offset + 2
    return 0
