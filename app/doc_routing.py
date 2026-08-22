"""Routage : quel document va dans quel onglet, avec quelle structure.

Les onglets EXISTANTS sont reutilises tels quels - jamais remplaces. Les
onglets manquants sont crees dans le style du classeur : en-tetes, formats
de date et de devise, validations, et identifiant interne stable.

Regle comptable structurante : un devis, un bon de commande ou un bon de
livraison ne cree NI chiffre d'affaires, NI TVA, NI charge. Ils sont suivis
dans un onglet documentaire dedie.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.doc_extract import ExtractedDocument
from app.doc_types import (
    BANK_STATEMENT,
    CLIENT_CREDIT_NOTE,
    DELIVERY_NOTE,
    EXPORT_INVOICE,
    IMPORT_INVOICE,
    LABELS,
    PAYMENT_RECEIPT,
    PENALTY_NOTICE,
    PURCHASE_INVOICE,
    PURCHASE_ORDER,
    QUOTE,
    SALES_INVOICE,
    SUPPLIER_CREDIT_NOTE,
    UNKNOWN,
)
from app.invoice_sheet import (
    DATE_PATTERN,
    MONEY_PATTERN,
    RATE_PATTERN,
    to_number,
    to_serial,
)

# --- onglets existants du classeur (constates par lecture) ----------------
TAB_CLIENTS = "02_CLIENTS"
TAB_SUPPLIERS = "03_FOURNISSEURS"
TAB_SALES = "04_FACTURES_VENTES"
TAB_PURCHASES = "05_FACTURES_ACHATS"
TAB_BANK = "06_RELEVE_BANCAIRE"
TAB_IMPORTS_LOG = "14_IMPORTS_LOG"
TAB_INVOICE_LINES = "16_LIGNES_FACTURES"

# --- onglets crees si absents ---------------------------------------------
TAB_CREDIT_NOTES = "17_AVOIRS"
TAB_COMMERCIAL_DOCS = "18_DOCUMENTS_COMMERCIAUX"
TAB_PAYABLES = "19_ECHEANCES_A_PAYER"
TAB_CUSTOMS = "20_DOUANE"


@dataclass
class TabSpec:
    """Structure complete d'un onglet a creer si le classeur ne l'a pas."""

    title: str
    headers: list[str]
    id_prefix: str
    date_columns: tuple[str, ...] = ()
    money_columns: tuple[str, ...] = ()
    rate_columns: tuple[str, ...] = ()
    validations: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def last_column(self) -> str:
        return chr(ord("A") + len(self.headers) - 1)


CREDIT_NOTES_SPEC = TabSpec(
    title=TAB_CREDIT_NOTES,
    headers=[
        "ID", "Date", "Numéro avoir", "Sens", "ID Tiers", "Tiers",
        "Facture d'origine", "Montant HT", "Taux TVA (%)", "Montant TVA",
        "Montant TTC", "Devise", "Statut imputation", "Lien Drive",
    ],
    id_prefix="AV",
    date_columns=("B",),
    money_columns=("H", "J", "K"),
    rate_columns=("I",),
    validations={
        "D": ("Fournisseur", "Client"),
        "M": ("A imputer", "Impute", "Refuse"),
    },
)

COMMERCIAL_DOCS_SPEC = TabSpec(
    title=TAB_COMMERCIAL_DOCS,
    headers=[
        "ID", "Date", "Type", "Numéro", "Tiers", "ICE Tiers",
        "Montant HT indicatif", "Montant TTC indicatif", "Devise",
        "Échéance / Validité", "Statut", "Lien Drive",
    ],
    id_prefix="DOC",
    date_columns=("B", "J"),
    money_columns=("G", "H"),
    validations={
        "C": ("Devis", "Bon de commande", "Bon de livraison"),
        "K": ("En cours", "Accepte", "Refuse", "Livre", "Clos"),
    },
)

PAYABLES_SPEC = TabSpec(
    title=TAB_PAYABLES,
    headers=[
        "ID", "Date avis", "Référence", "Organisme", "Motif", "Montant",
        "Devise", "Échéance", "Statut", "Événement Calendar", "Lien Drive",
    ],
    id_prefix="ECH",
    date_columns=("B", "H"),
    money_columns=("F",),
    validations={"I": ("A payer", "Payee", "Contestee")},
)

CUSTOMS_SPEC = TabSpec(
    title=TAB_CUSTOMS,
    headers=[
        "ID", "ID Facture", "Sens", "Numéro", "Date", "Devise", "Incoterm",
        "Pays d'origine", "Pays de destination", "Codes SH",
        "Valeur marchandises", "Fret et assurance", "Total", "Lien Drive",
    ],
    id_prefix="DOU",
    date_columns=("E",),
    money_columns=("K", "L", "M"),
    validations={"C": ("Import", "Export")},
)

NEW_TAB_SPECS = {
    TAB_CREDIT_NOTES: CREDIT_NOTES_SPEC,
    TAB_COMMERCIAL_DOCS: COMMERCIAL_DOCS_SPEC,
    TAB_PAYABLES: PAYABLES_SPEC,
    TAB_CUSTOMS: CUSTOMS_SPEC,
}


@dataclass
class Route:
    """Ou va le document, et ce qu'il declenche."""

    primary_tab: str
    accounting: bool                    # cree une ecriture comptable
    drive_folder: str
    extra_tabs: tuple[str, ...] = ()
    calendar: bool = False
    reconcile: bool = False


ROUTES: dict[str, Route] = {
    PURCHASE_INVOICE: Route(
        TAB_PURCHASES, True, "Factures achats",
        extra_tabs=(TAB_INVOICE_LINES, TAB_SUPPLIERS),
    ),
    SALES_INVOICE: Route(
        TAB_SALES, True, "Factures ventes",
        extra_tabs=(TAB_INVOICE_LINES, TAB_CLIENTS),
    ),
    SUPPLIER_CREDIT_NOTE: Route(TAB_CREDIT_NOTES, True, "Avoirs"),
    CLIENT_CREDIT_NOTE: Route(TAB_CREDIT_NOTES, True, "Avoirs"),
    QUOTE: Route(TAB_COMMERCIAL_DOCS, False, "Devis"),
    PURCHASE_ORDER: Route(TAB_COMMERCIAL_DOCS, False, "Bons de commande"),
    DELIVERY_NOTE: Route(TAB_COMMERCIAL_DOCS, False, "Bons de livraison"),
    BANK_STATEMENT: Route(TAB_BANK, True, "Releves bancaires"),
    PAYMENT_RECEIPT: Route(TAB_BANK, True, "Paiements", reconcile=True),
    PENALTY_NOTICE: Route(TAB_PAYABLES, True, "Penalites et taxes", calendar=True),
    IMPORT_INVOICE: Route(
        TAB_PURCHASES, True, "Import",
        extra_tabs=(TAB_INVOICE_LINES, TAB_SUPPLIERS, TAB_CUSTOMS),
    ),
    EXPORT_INVOICE: Route(
        TAB_SALES, True, "Export",
        extra_tabs=(TAB_INVOICE_LINES, TAB_CLIENTS, TAB_CUSTOMS),
    ),
    UNKNOWN: Route("", False, "A verifier"),
}

# Dossiers Drive crees ou reutilises, par annee.
DRIVE_FOLDERS = tuple(sorted({r.drive_folder for r in ROUTES.values()}))


# Colonne du lien Drive dans chaque onglet qui en possede une. Les onglets
# factures du client n'en ont pas : pour eux, le lien vit dans le journal
# d'import, jamais dans une colonne inventee.
DRIVE_LINK_COLUMN = {
    TAB_CREDIT_NOTES: "N",
    TAB_COMMERCIAL_DOCS: "L",
    TAB_PAYABLES: "K",
    TAB_CUSTOMS: "N",
}
CALENDAR_EVENT_COLUMN = {TAB_PAYABLES: "J"}


def route_for(doc_type: str) -> Route:
    return ROUTES.get(doc_type, ROUTES[UNKNOWN])


def is_purchase_side(doc_type: str) -> bool:
    return doc_type in (PURCHASE_INVOICE, IMPORT_INVOICE, SUPPLIER_CREDIT_NOTE, PURCHASE_ORDER)


# --- constructeurs de lignes pour les nouveaux onglets --------------------

def build_credit_note_row(
    *, stable_id: str, doc: ExtractedDocument, party_id: str, drive_link: str = ""
) -> list[object]:
    """Ligne d'avoir. Les montants sont deja signes negativement a
    l'extraction : ils ne sont jamais confondus avec une facture."""
    sens = "Fournisseur" if doc.doc_type == SUPPLIER_CREDIT_NOTE else "Client"
    tiers = doc.emetteur if sens == "Fournisseur" else doc.destinataire
    return [
        stable_id,
        to_serial(doc.date_document) if doc.date_document else "",
        doc.numero or "",
        sens,
        party_id,
        tiers or "",
        doc.facture_liee or "",
        to_number(doc.montant_ht.value) if doc.montant_ht else "",
        to_number(doc.taux_tva) if doc.taux_tva is not None else "",
        to_number(doc.montant_tva.value) if doc.montant_tva else "",
        to_number(doc.montant_ttc.value) if doc.montant_ttc else "",
        doc.devise,
        "A imputer",
        drive_link,
    ]


def build_commercial_row(
    *, stable_id: str, doc: ExtractedDocument, drive_link: str = ""
) -> list[object]:
    """Ligne de suivi documentaire : aucun impact comptable.

    Les montants sont explicitement qualifies d'INDICATIFS : ils ne doivent
    jamais alimenter le chiffre d'affaires, la TVA ni les charges.
    """
    tiers = doc.emetteur or doc.destinataire or ""
    ice = doc.emetteur_ice or doc.destinataire_ice or ""
    return [
        stable_id,
        to_serial(doc.date_document) if doc.date_document else "",
        LABELS.get(doc.doc_type, doc.doc_type),
        doc.numero or "",
        tiers,
        ice,
        to_number(doc.montant_ht.value) if doc.montant_ht else "",
        to_number(doc.montant_ttc.value) if doc.montant_ttc else "",
        doc.devise,
        to_serial(doc.date_echeance) if doc.date_echeance else "",
        "En cours",
        drive_link,
    ]


def build_payable_row(
    *, stable_id: str, doc: ExtractedDocument, calendar_event: str = "",
    drive_link: str = "", motif: str = "",
) -> list[object]:
    return [
        stable_id,
        to_serial(doc.date_document) if doc.date_document else "",
        doc.numero or "",
        doc.emetteur or "",
        motif,
        to_number(doc.montant_ttc.value) if doc.montant_ttc else "",
        doc.devise,
        to_serial(doc.date_echeance) if doc.date_echeance else "",
        "A payer",
        calendar_event,
        drive_link,
    ]


def build_customs_row(
    *, stable_id: str, invoice_id: str, doc: ExtractedDocument,
    freight: Decimal | None = None, drive_link: str = "",
) -> list[object]:
    """Ligne douaniere. Une cellule sans valeur s'ecrit "", jamais None :
    l'API Sheets ecrirait sinon une cellule de type nul difficile a relire."""
    sens = "Import" if doc.doc_type == IMPORT_INVOICE else "Export"
    hs_codes = ", ".join(sorted({l.hs_code for l in doc.lignes if l.hs_code}))
    return [
        stable_id,
        invoice_id,
        sens,
        doc.numero or "",
        to_serial(doc.date_document) if doc.date_document else "",
        doc.devise,
        doc.incoterm,
        doc.pays_origine,
        doc.pays_destination,
        hs_codes,
        to_number(doc.montant_ht.value) if doc.montant_ht else "",
        to_number(freight) if freight is not None else "",
        to_number(doc.montant_ttc.value) if doc.montant_ttc else "",
        drive_link,
    ]


def build_bank_rows(
    *, start_index: int, doc: ExtractedDocument, account: str = "Banque Principale DEMO"
) -> list[list[object]]:
    """Une ligne de classeur par operation, au format reel de 06_RELEVE_BANCAIRE.

    Colonnes : ID, Compte, Date operation, Date valeur, Libelle, Reference,
    Debit, Credit, Solde, Tiers, Categorie, Facture liee, Statut.
    """
    rows: list[list[object]] = []
    for offset, line in enumerate(doc.bank_lines):
        serial = to_serial(line.date_operation) if line.date_operation else ""
        rows.append([
            f"TX-{start_index + offset:04d}",
            account,
            serial,
            serial,
            line.libelle,
            line.reference,
            to_number(line.debit) if line.debit is not None else "",
            to_number(line.credit) if line.credit is not None else "",
            to_number(line.solde),
            "",
            "",
            "",
            "Non rapproche",
        ])
    return rows


def bank_line_fingerprint(account: str, line) -> str:
    """Empreinte stable d'une operation bancaire, pour l'anti-doublon.

    Date + libelle + montant + solde : deux releves qui se chevauchent ne
    creent pas deux fois la meme operation.
    """
    import hashlib

    raw = "|".join((
        account,
        line.date_operation.isoformat() if line.date_operation else "",
        (line.libelle or "").strip().upper(),
        str(line.debit or ""),
        str(line.credit or ""),
        str(line.solde or ""),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
