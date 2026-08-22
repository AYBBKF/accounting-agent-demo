"""Construction DETERMINISTE des lignes ecrites dans le classeur.

Ce module ne fait aucun appel reseau : il transforme des donnees deja
extraites en cellules pretes a ecrire, en respectant les conventions
REELLES du classeur du client (constatees par lecture, pas supposees) :

  - colonne A : identifiant stable "FA-<annee>-<sequence>" (JAMAIS le
    numero de facture, qui vient du fournisseur et n'est pas maitrise) ;
  - colonne D : identifiant du fournisseur ("FRS-00N") ;
  - colonnes G, I, J, K, L, O : nombres natifs au format "#,##0.00 \\"MAD\\"" ;
  - colonne H : taux de TVA en nombre entier (20), au format "0\\"%\\"" -
    surtout PAS 0,2 en format pourcentage, qui casse la lecture ;
  - colonnes B, N : dates en numero de serie Google Sheets, format
    "yyyy-mm-dd" ;
  - colonnes K, L, M, Q : formules, recopiees a l'identique du modele ;
  - colonne P : statut issu de la liste de validation du classeur.

Les montants restent des Decimal jusqu'a la derniere etape. La conversion
en float n'a lieu qu'au moment de serialiser vers l'API Google Sheets, qui
ne connait que des nombres JSON - et qui stocke de toute facon des doubles.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# Origine des numeros de serie Google Sheets.
_SHEETS_EPOCH = date(1899, 12, 30)

# Formats reels releves sur les lignes existantes du classeur.
MONEY_PATTERN = '#,##0.00 "MAD"'
RATE_PATTERN = '0"%"'
DATE_PATTERN = "yyyy-mm-dd"

MONEY_COLUMNS = ("G", "I", "J", "K", "L", "O")
DATE_COLUMNS = ("B", "N")
RATE_COLUMN = "H"
STATUS_COLUMN = "P"

# Liste de validation reelle de la colonne Statut.
STATUS_VALUES = ("Payee", "Partiellement payee", "Impayee")
STATUS_UNPAID = "Impayee"
STATUS_PAID = "Payee"
STATUS_PARTIAL = "Partiellement payee"

LIGNES_TAB = "16_LIGNES_FACTURES"
LIGNES_HEADERS = [
    "ID facture",
    "Onglet facture",
    "Numéro facture",
    "N° ligne",
    "Description",
    "Quantité",
    "Prix unitaire HT",
    "Taux TVA (%)",
    "Total HT",
]

IMPORTS_LOG_TAB = "14_IMPORTS_LOG"


class InvoiceSheetError(RuntimeError):
    pass


def to_serial(value: date) -> int:
    """Date -> numero de serie Google Sheets (comme les lignes existantes)."""
    return (value - _SHEETS_EPOCH).days


def to_number(value: Decimal | None) -> float | None:
    """Decimal -> nombre JSON, uniquement au moment de l'ecriture."""
    return None if value is None else float(value)


def _sequence(existing: list[str], pattern: re.Pattern[str]) -> int:
    highest = 0
    for value in existing:
        m = pattern.fullmatch((value or "").strip())
        if m:
            highest = max(highest, int(m.group("seq")))
    return highest


def next_stable_invoice_id(existing_ids: list[str], year: int, prefix: str = "FA") -> str:
    """Prochain identifiant stable, dans la continuite de ceux du classeur.

    La sequence est globale au prefixe (et non par annee) afin de ne jamais
    reutiliser un identifiant deja attribue si l'annee change.
    """
    pattern = re.compile(rf"{re.escape(prefix)}-(?P<year>\d{{4}})-(?P<seq>\d+)")
    width = 3
    for value in existing_ids:
        m = pattern.fullmatch((value or "").strip())
        if m:
            width = max(width, len(m.group("seq")))
    return f"{prefix}-{year:04d}-{_sequence(existing_ids, pattern) + 1:0{width}d}"


def next_supplier_id(existing_ids: list[str], prefix: str = "FRS") -> str:
    pattern = re.compile(rf"{re.escape(prefix)}-(?P<seq>\d+)")
    width = 3
    for value in existing_ids:
        m = pattern.fullmatch((value or "").strip())
        if m:
            width = max(width, len(m.group("seq")))
    return f"{prefix}-{_sequence(existing_ids, pattern) + 1:0{width}d}"


def normalize_status(raw: str | None, *, montant_paye: Decimal | None = None) -> str:
    """Ramene un statut de facture au vocabulaire du classeur.

    En l'absence d'information de paiement fiable, le statut est "Impayee" :
    c'est le choix prudent pour un suivi comptable.
    """
    if montant_paye is not None:
        return STATUS_UNPAID if montant_paye == 0 else STATUS_PAID
    folded = "".join(
        c for c in unicodedata.normalize("NFD", raw or "")
        if unicodedata.category(c) != "Mn"
    )
    text = re.sub(r"[^A-Z]", "", folded.upper())
    if not text:
        return STATUS_UNPAID
    if "PARTIEL" in text:
        return STATUS_PARTIAL
    if "NONPAY" in text or "IMPAY" in text or "NONREGL" in text:
        return STATUS_UNPAID
    if "PAYE" in text or "REGLE" in text or "ACQUITT" in text:
        return STATUS_PAID
    return STATUS_UNPAID


def normalize_vat_rate(rate: Decimal | None) -> Decimal | None:
    """Taux de TVA au format REEL du classeur : 20 (et non 0,2).

    Un PDF peut annoncer "20 %" ou "0,20". Les deux designent le meme taux ;
    le classeur stocke 20 avec le format d'affichage 0"%".
    """
    if rate is None:
        return None
    if rate < 1:
        return rate * Decimal("100")
    return rate


@dataclass
class InvoiceRowPlan:
    """Tout ce qu'il faut ecrire pour une ligne de facture, par plage."""

    row_index: int              # numero de ligne 1-based dans l'onglet
    tab: str
    stable_id: str
    values_a_j: list[object]    # A..J  (ecriture RAW : types natifs)
    values_n_p: list[object]    # N..P  (ecriture RAW)
    formulas_k_m: list[str]     # K..M  (ecriture USER_ENTERED)
    formula_q: str              # Q     (ecriture USER_ENTERED)

    @property
    def range_a_j(self) -> str:
        return f"{self.tab}!A{self.row_index}:J{self.row_index}"

    @property
    def range_n_p(self) -> str:
        return f"{self.tab}!N{self.row_index}:P{self.row_index}"

    @property
    def range_k_m(self) -> str:
        return f"{self.tab}!K{self.row_index}:M{self.row_index}"

    @property
    def range_q(self) -> str:
        return f"{self.tab}!Q{self.row_index}"


def build_row_plan(
    *,
    tab: str,
    row_index: int,
    stable_id: str,
    supplier_id: str,
    supplier_name: str,
    numero: str,
    description: str,
    date_facture: date,
    date_echeance: date | None,
    montant_ht: Decimal,
    taux_tva: Decimal | None,
    montant_tva: Decimal,
    montant_ttc: Decimal,
    montant_paye: Decimal = Decimal("0"),
    statut: str | None = None,
    last_data_row: int | None = None,
) -> InvoiceRowPlan:
    """Construit la ligne complete, colonnes calculees comprises.

    `last_data_row` borne la plage du controle de doublon (colonne M) ; par
    defaut la ligne courante, ce qui etend la plage sans jamais toucher aux
    formules des lignes deja presentes.
    """
    if row_index < 2:
        raise InvoiceSheetError("La ligne 1 est la ligne d'en-tetes.")
    last = last_data_row or row_index
    n = row_index

    values_a_j: list[object] = [
        stable_id,
        to_serial(date_facture),
        numero,
        supplier_id,
        supplier_name,
        description,
        to_number(montant_ht),
        to_number(normalize_vat_rate(taux_tva)),
        to_number(montant_tva),
        to_number(montant_ttc),
    ]
    values_n_p: list[object] = [
        to_serial(date_echeance) if date_echeance else "",
        to_number(montant_paye),
        normalize_status(statut, montant_paye=montant_paye),
    ]
    formulas_k_m = [
        f"=ROUND(G{n}+I{n};2)",
        f"=J{n}-K{n}",
        f'=IF(COUNTIF($C$2:$C${last};C{n})>1;"DOUBLON";"")',
    ]
    formula_q = f"=IF(AND(O{n}<J{n};TODAY()>N{n});TODAY()-N{n};0)"

    return InvoiceRowPlan(
        row_index=row_index,
        tab=tab,
        stable_id=stable_id,
        values_a_j=values_a_j,
        values_n_p=values_n_p,
        formulas_k_m=formulas_k_m,
        formula_q=formula_q,
    )


def build_line_rows(
    *, stable_id: str, tab: str, numero: str, lignes: list
) -> list[list[object]]:
    """Lignes de detail pour l'onglet 16_LIGNES_FACTURES."""
    rows: list[list[object]] = []
    for index, ligne in enumerate(lignes, start=1):
        rows.append(
            [
                stable_id,
                tab,
                numero,
                index,
                ligne.description,
                to_number(ligne.quantite),
                to_number(ligne.prix_unitaire_ht),
                to_number(normalize_vat_rate(ligne.taux_tva)),
                to_number(ligne.total_ht),
            ]
        )
    return rows


def build_import_log_row(
    *,
    horodatage: str,
    stable_id: str,
    action: str,
    statut: str,
    numero: str,
    fournisseur: str,
    ice: str,
    montant_ht: Decimal | None,
    montant_tva: Decimal | None,
    montant_ttc: Decimal | None,
    tab: str,
    row_index: int,
    gmail_message_id: str = "",
    gmail_expediteur: str = "",
    gmail_objet: str = "",
    piece_jointe: str = "",
    drive_lien: str = "",
    type_enregistrement: str = "Facture achat",
    avertissements: tuple[str, ...] | list[str] = (),
    en_attente: bool = False,
) -> list[object]:
    """Entree complete du journal d'import (6 colonnes reelles de l'onglet).

    Les informations Gmail et le lien Drive sont conserves dans le detail :
    l'onglet existant n'a que six colonnes et on ne modifie pas sa structure.

    `en_attente` marque un document archive mais NON comptabilise : le
    journal doit distinguer "importe" de "depose dans A verifier, ecriture
    en attente de decision", sinon le client ne peut pas savoir ce qui
    manque reellement a sa comptabilite.
    """
    detail_parts = [
        "ECRITURE COMPTABLE EN ATTENTE DE VALIDATION" if en_attente else "",
        *(f"Avertissement : {w}" for w in avertissements),
        f"Facture {numero}",
        f"Fournisseur {fournisseur} (ICE {ice})" if ice else f"Fournisseur {fournisseur}",
        f"HT {montant_ht} MAD" if montant_ht is not None else "",
        f"TVA {montant_tva} MAD" if montant_tva is not None else "",
        f"TTC {montant_ttc} MAD" if montant_ttc is not None else "",
        f"Onglet {tab} ligne {row_index}",
        f"Gmail message {gmail_message_id}" if gmail_message_id else "",
        f"Expediteur {gmail_expediteur}" if gmail_expediteur else "",
        f"Objet {gmail_objet}" if gmail_objet else "",
        f"Piece jointe {piece_jointe}" if piece_jointe else "",
        f"Drive {drive_lien}" if drive_lien else "",
    ]
    detail = " | ".join(part for part in detail_parts if part)
    return [horodatage, type_enregistrement, stable_id, action, statut, detail]
