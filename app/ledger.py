"""Journal comptable en partie double, par entreprise.

Le classeur XBLASTE porte deja un plan comptable, declare dans son onglet
`01_PARAMETRES` et employe par les ecritures existantes de
`12_JOURNAL_COMPTABLE` : 3421 Client, 4411 Fournisseur, 7111 Vente,
6111 Achat, 4455 TVA collectee, 3455 TVA deductible, journaux VE/AC/OD.
Ce module ne reinvente RIEN de tout cela : il genere des ecritures dans
ce plan, et refuse d'inventer un compte qui n'y figure pas.

Trois regles gouvernent chaque ecriture :

  * PARTIE DOUBLE STRICTE. Pour chaque piece et chaque devise,
    somme(debit) == somme(credit), au centime, en Decimal. Une ecriture
    qui ne s'equilibre pas n'est jamais posee : elle leve, avec l'ecart
    exact, et l'appelant met la piece en quarantaine.

  * AUCUN COMPTE INVENTE. Le plan vient du template et peut etre
    complete PAR SOCIETE via la configuration (`account_mapping` dans
    COMPANIES_JSON). Quand une operation exige un compte absent - la
    banque ou les frais bancaires, que le template ne declare pas -
    l'ecriture est enregistree au statut A_VALIDER, hors du classeur,
    en attendant que l'exploitant fournisse le compte. Elle n'est
    jamais presentee comme definitive.

  * IDEMPOTENCE PAR PIECE. La cle primaire (entreprise, piece, ligne)
    fait qu'un retraitement du meme document ne peut pas produire une
    seconde ecriture : le rejeu est un non-evenement.

L'historique ne se corrige jamais par suppression : une annulation est
une ecriture d'EXTOURNE (debits et credits inverses, piece suffixee),
qui laisse l'originale intacte.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

# Journaux du template.
JOURNAL_VENTES = "VE"
JOURNAL_ACHATS = "AC"
JOURNAL_BANQUE = "BQ"
JOURNAL_OD = "OD"

STATUT_VALIDEE = "VALIDEE"
STATUT_A_VALIDER = "A_VALIDER"
STATUT_EXTOURNEE = "EXTOURNEE"

# Plan de comptes du template XBLASTE (01_PARAMETRES). Les libelles sont
# ceux que le journal du classeur utilise deja.
TEMPLATE_ACCOUNTS = {
    "client": ("3421", "Client"),
    "fournisseur": ("4411", "Fournisseur"),
    "vente": ("7111", "Vente"),
    "achat": ("6111", "Achat"),
    "tva_collectee": ("4455", "TVA collectée"),
    "tva_deductible": ("3455", "TVA déductible"),
    # Volontairement ABSENTS du defaut : le template ne les declare pas.
    # Ils se fournissent par societe (account_mapping) ; sans eux, les
    # operations bancaires partent en A_VALIDER.
    # "banque": ("5141", "Banque"),
    # "frais_bancaires": ("6147", "Services bancaires"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_entries (
    company_id TEXT NOT NULL,
    piece TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    journal TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    compte TEXT NOT NULL DEFAULT '',
    libelle TEXT NOT NULL DEFAULT '',
    tiers TEXT NOT NULL DEFAULT '',
    debit TEXT NOT NULL DEFAULT '0',
    credit TEXT NOT NULL DEFAULT '0',
    devise TEXT NOT NULL DEFAULT 'MAD',
    taux_tva TEXT NOT NULL DEFAULT '',
    montant_tva TEXT NOT NULL DEFAULT '',
    reference TEXT NOT NULL DEFAULT '',
    doc_sha256 TEXT NOT NULL DEFAULT '',
    gmail_message_id TEXT NOT NULL DEFAULT '',
    drive_file_id TEXT NOT NULL DEFAULT '',
    statut TEXT NOT NULL DEFAULT 'VALIDEE',
    motif TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (company_id, piece, line_no)
);
CREATE INDEX IF NOT EXISTS idx_ledger_company_periode
    ON ledger_entries(company_id, entry_date);
"""


class LedgerError(RuntimeError):
    """Ecriture impossible a poser telle quelle."""


class LedgerImbalance(LedgerError):
    """Somme debit != somme credit : refus, avec l'ecart exact."""


@dataclass(frozen=True)
class Line:
    compte: str
    libelle: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")


@dataclass
class Entry:
    """Une piece complete, prete a poser."""

    company_id: str
    journal: str
    piece: str
    entry_date: str
    lines: list[Line]
    tiers: str = ""
    devise: str = "MAD"
    taux_tva: str = ""
    montant_tva: str = ""
    reference: str = ""
    doc_sha256: str = ""
    gmail_message_id: str = ""
    drive_file_id: str = ""
    statut: str = STATUT_VALIDEE
    motif: str = ""


class AccountMapping:
    """Plan de comptes d'UNE societe : template + complements declares.

    `overrides` vient de la configuration de la societe. Une cle inconnue
    du template et non fournie ne renvoie rien : c'est a l'appelant de
    degrader l'ecriture en A_VALIDER, jamais d'inventer un numero.
    """

    def __init__(self, overrides: dict[str, object] | None = None) -> None:
        self._plan: dict[str, tuple[str, str]] = dict(TEMPLATE_ACCOUNTS)
        for cle, valeur in (overrides or {}).items():
            if isinstance(valeur, (list, tuple)) and len(valeur) == 2:
                self._plan[str(cle)] = (str(valeur[0]), str(valeur[1]))
            elif isinstance(valeur, str) and valeur.strip():
                self._plan[str(cle)] = (valeur.strip(), str(cle))

    def get(self, role: str) -> tuple[str, str] | None:
        return self._plan.get(role)

    def missing(self, *roles: str) -> tuple[str, ...]:
        return tuple(r for r in roles if r not in self._plan)


def ensure_schema(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _verify_balance(piece: str, lines: list[Line]) -> None:
    debit = sum((l.debit for l in lines), Decimal("0"))
    credit = sum((l.credit for l in lines), Decimal("0"))
    if debit != credit:
        raise LedgerImbalance(
            f"ecriture {piece} desequilibree : debit {debit} != credit {credit} "
            f"(ecart {abs(debit - credit)})"
        )
    if not lines:
        raise LedgerError(f"ecriture {piece} sans aucune ligne")


# --- generateurs d'ecritures, par evenement comptable ----------------------
#
# Une facture et son paiement sont DEUX evenements : la facture pose la
# dette (ou la creance), le paiement la solde via la banque. Le
# rapprochement ne recree jamais la facture.

def _tva_lines(ht: Decimal, tva: Decimal, ttc: Decimal, piece: str) -> None:
    if ht + tva != ttc:
        raise LedgerImbalance(
            f"ecriture {piece} : HT {ht} + TVA {tva} != TTC {ttc} "
            f"(ecart {abs(ht + tva - ttc)})"
        )


def purchase_invoice(m: AccountMapping, *, piece: str, ht: Decimal,
                     tva: Decimal, ttc: Decimal) -> list[Line]:
    _tva_lines(ht, tva, ttc, piece)
    achat, fournisseur = m.get("achat"), m.get("fournisseur")
    deductible = m.get("tva_deductible")
    lignes = [Line(*achat, debit=ht)]
    if tva:
        lignes.append(Line(*deductible, debit=tva))
    lignes.append(Line(*fournisseur, credit=ttc))
    return lignes


def sales_invoice(m: AccountMapping, *, piece: str, ht: Decimal,
                  tva: Decimal, ttc: Decimal) -> list[Line]:
    _tva_lines(ht, tva, ttc, piece)
    client, vente = m.get("client"), m.get("vente")
    collectee = m.get("tva_collectee")
    lignes = [Line(*client, debit=ttc), Line(*vente, credit=ht)]
    if tva:
        lignes.append(Line(*collectee, credit=tva))
    return lignes


def supplier_credit_note(m: AccountMapping, *, piece: str, ht: Decimal,
                         tva: Decimal, ttc: Decimal) -> list[Line]:
    """Avoir fournisseur : inverse exactement la facture d'achat."""
    _tva_lines(ht, tva, ttc, piece)
    achat, fournisseur = m.get("achat"), m.get("fournisseur")
    deductible = m.get("tva_deductible")
    lignes = [Line(*fournisseur, debit=ttc), Line(*achat, credit=ht)]
    if tva:
        lignes.append(Line(*deductible, credit=tva))
    return lignes


def client_credit_note(m: AccountMapping, *, piece: str, ht: Decimal,
                       tva: Decimal, ttc: Decimal) -> list[Line]:
    """Avoir client : inverse exactement la facture de vente."""
    _tva_lines(ht, tva, ttc, piece)
    client, vente = m.get("client"), m.get("vente")
    collectee = m.get("tva_collectee")
    lignes = [Line(*vente, debit=ht)]
    if tva:
        lignes.append(Line(*collectee, debit=tva))
    lignes.append(Line(*client, credit=ttc))
    return lignes


def supplier_payment(m: AccountMapping, *, montant: Decimal) -> list[Line]:
    banque, fournisseur = m.get("banque"), m.get("fournisseur")
    return [Line(*fournisseur, debit=montant), Line(*banque, credit=montant)]


def client_settlement(m: AccountMapping, *, montant: Decimal) -> list[Line]:
    banque, client = m.get("banque"), m.get("client")
    return [Line(*banque, debit=montant), Line(*client, credit=montant)]


def bank_fee(m: AccountMapping, *, montant: Decimal) -> list[Line]:
    banque, frais = m.get("banque"), m.get("frais_bancaires")
    return [Line(*frais, debit=montant), Line(*banque, credit=montant)]


_BESOINS = {
    "facture_achat": ("achat", "fournisseur", "tva_deductible"),
    "facture_vente": ("client", "vente", "tva_collectee"),
    "avoir_fournisseur": ("achat", "fournisseur", "tva_deductible"),
    "avoir_client": ("client", "vente", "tva_collectee"),
    "paiement_fournisseur": ("banque", "fournisseur"),
    "reglement_client": ("banque", "client"),
    "frais_bancaires": ("banque", "frais_bancaires"),
}
_GENERATEURS = {
    "facture_achat": (JOURNAL_ACHATS, purchase_invoice),
    "facture_vente": (JOURNAL_VENTES, sales_invoice),
    "avoir_fournisseur": (JOURNAL_ACHATS, supplier_credit_note),
    "avoir_client": (JOURNAL_VENTES, client_credit_note),
}
_GENERATEURS_BANQUE = {
    "paiement_fournisseur": supplier_payment,
    "reglement_client": client_settlement,
    "frais_bancaires": bank_fee,
}


def build_entry(
    mapping: AccountMapping, *, company_id: str, kind: str, piece: str,
    entry_date: str, devise: str = "MAD",
    ht: Decimal | None = None, tva: Decimal | None = None,
    ttc: Decimal | None = None, montant: Decimal | None = None,
    tiers: str = "", taux_tva: str = "", reference: str = "",
    doc_sha256: str = "", gmail_message_id: str = "", drive_file_id: str = "",
) -> Entry:
    """Construit l'ecriture d'UN evenement, equilibree ou degradee.

    Un compte manquant ne fait pas echouer : il degrade l'ecriture au
    statut A_VALIDER (aucune ligne posee, motif exact conserve). Un
    desequilibre, lui, leve : il signale une donnee fausse, pas une
    configuration incomplete.
    """
    if kind not in _BESOINS:
        raise LedgerError(f"evenement comptable inconnu : '{kind}'")
    manquants = mapping.missing(*_BESOINS[kind])
    entry = Entry(
        company_id=company_id, journal="", piece=piece, entry_date=entry_date,
        lines=[], tiers=tiers, devise=devise, taux_tva=taux_tva,
        montant_tva=str(tva) if tva is not None else "",
        reference=reference, doc_sha256=doc_sha256,
        gmail_message_id=gmail_message_id, drive_file_id=drive_file_id,
    )
    if manquants:
        entry.journal = JOURNAL_OD
        entry.statut = STATUT_A_VALIDER
        entry.motif = (
            "compte(s) non declare(s) pour cette societe : "
            + ", ".join(manquants)
            + " - a fournir via account_mapping ; aucun compte n'a ete invente"
        )
        return entry

    if kind in _GENERATEURS:
        journal, generateur = _GENERATEURS[kind]
        entry.journal = journal
        entry.lines = generateur(
            mapping, piece=piece,
            ht=ht or Decimal("0"), tva=tva or Decimal("0"), ttc=ttc or Decimal("0"),
        )
    else:
        entry.journal = JOURNAL_BANQUE
        entry.lines = _GENERATEURS_BANQUE[kind](mapping, montant=montant or Decimal("0"))

    _verify_balance(piece, entry.lines)
    return entry


def reversal_entry(original: Entry, *, motif: str) -> Entry:
    """Extourne : l'annulation qui n'efface rien.

    Debits et credits inverses, piece suffixee `-EXT`. L'originale reste
    dans l'historique ; la somme des deux est nulle.
    """
    lignes = [
        Line(l.compte, f"Extourne - {l.libelle}", debit=l.credit, credit=l.debit)
        for l in original.lines
    ]
    contre = Entry(
        company_id=original.company_id, journal=original.journal,
        piece=f"{original.piece}-EXT", entry_date=original.entry_date,
        lines=lignes, tiers=original.tiers, devise=original.devise,
        reference=original.piece, doc_sha256=original.doc_sha256,
        gmail_message_id=original.gmail_message_id,
        drive_file_id=original.drive_file_id, motif=motif,
    )
    _verify_balance(contre.piece, contre.lines)
    return contre


# --- persistance ------------------------------------------------------------


def record(db_path: str, entry: Entry) -> bool:
    """Pose l'ecriture. Rend False si la piece existe deja (rejeu).

    L'idempotence est structurelle : la cle primaire refuse la seconde
    pose, et on la detecte AVANT pour ne rien ecrire du tout.
    """
    if not entry.company_id.strip():
        raise LedgerError("une ecriture doit appartenir a une entreprise")
    if entry.statut == STATUT_VALIDEE:
        _verify_balance(entry.piece, entry.lines)
    ensure_schema(db_path)
    maintenant = _now()
    with sqlite3.connect(db_path) as conn:
        existe = conn.execute(
            "SELECT 1 FROM ledger_entries WHERE company_id=? AND piece=? LIMIT 1",
            (entry.company_id, entry.piece),
        ).fetchone()
        if existe:
            return False
        lignes = entry.lines or [Line("", entry.motif or "en attente de compte")]
        for numero, ligne in enumerate(lignes, start=1):
            conn.execute(
                "INSERT INTO ledger_entries (company_id, piece, line_no, journal,"
                " entry_date, compte, libelle, tiers, debit, credit, devise,"
                " taux_tva, montant_tva, reference, doc_sha256, gmail_message_id,"
                " drive_file_id, statut, motif, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.company_id, entry.piece, numero, entry.journal,
                    entry.entry_date, ligne.compte, ligne.libelle, entry.tiers,
                    str(ligne.debit), str(ligne.credit), entry.devise,
                    entry.taux_tva, entry.montant_tva, entry.reference,
                    entry.doc_sha256, entry.gmail_message_id,
                    entry.drive_file_id, entry.statut, entry.motif, maintenant,
                ),
            )
        conn.commit()
    return True


def entries_for(db_path: str, company_id: str, piece: str = "") -> list[dict]:
    ensure_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        requete = "SELECT * FROM ledger_entries WHERE company_id=?"
        params: list[object] = [company_id]
        if piece:
            requete += " AND piece=?"
            params.append(piece)
        requete += " ORDER BY piece, line_no"
        return [dict(r) for r in conn.execute(requete, params)]


def balance_report(db_path: str, company_id: str) -> dict[str, dict[str, Decimal]]:
    """Equilibre par piece et devise, pour les ecritures VALIDEES.

    C'est la preuve d'audit : chaque piece doit rendre un ecart nul.
    """
    sortie: dict[str, dict[str, Decimal]] = {}
    for ligne in entries_for(db_path, company_id):
        if ligne["statut"] != STATUT_VALIDEE:
            continue
        cle = f"{ligne['piece']} ({ligne['devise']})"
        agregat = sortie.setdefault(cle, {"debit": Decimal("0"), "credit": Decimal("0")})
        agregat["debit"] += Decimal(ligne["debit"])
        agregat["credit"] += Decimal(ligne["credit"])
    for agregat in sortie.values():
        agregat["ecart"] = agregat["debit"] - agregat["credit"]
    return sortie


def tva_recap(db_path: str, company_id: str) -> list[dict[str, str]]:
    """Recapitulatif TVA par periode (AAAA-MM), ecritures VALIDEES seules.

    Les quarantaines n'ont pose aucune ecriture et les doublons n'ont
    jamais ete enregistres deux fois : ils sont exclus par construction,
    pas par filtrage.
    """
    mapping = AccountMapping()
    collectee = (mapping.get("tva_collectee") or ("", ""))[0]
    deductible = (mapping.get("tva_deductible") or ("", ""))[0]
    periodes: dict[str, dict[str, Decimal]] = {}
    for ligne in entries_for(db_path, company_id):
        if ligne["statut"] != STATUT_VALIDEE:
            continue
        periode = str(ligne["entry_date"])[:7]
        agregat = periodes.setdefault(
            periode, {"collectee": Decimal("0"), "deductible": Decimal("0")}
        )
        if ligne["compte"] == collectee:
            agregat["collectee"] += Decimal(ligne["credit"]) - Decimal(ligne["debit"])
        elif ligne["compte"] == deductible:
            agregat["deductible"] += Decimal(ligne["debit"]) - Decimal(ligne["credit"])
    sortie = []
    for periode in sorted(periodes):
        agregat = periodes[periode]
        sortie.append({
            "periode": periode,
            "tva_collectee": str(agregat["collectee"]),
            "tva_deductible": str(agregat["deductible"]),
            "tva_due": str(agregat["collectee"] - agregat["deductible"]),
        })
    return sortie


def sheet_rows(entry: Entry) -> list[list[str]]:
    """Projection A..G du journal du classeur : Date | Journal | Piece |
    Compte | Libelle Compte | Debit | Credit. Seules les ecritures
    VALIDEES ont une projection ; une A_VALIDER n'entre pas au classeur."""
    if entry.statut != STATUT_VALIDEE:
        return []
    lignes = []
    for l in entry.lines:
        lignes.append([
            entry.entry_date, entry.journal, entry.piece, l.compte, l.libelle,
            str(l.debit) if l.debit else "", str(l.credit) if l.credit else "",
        ])
    return lignes
