"""Registre central des entreprises servies par l'agent.

Une seule instance de l'agent sert plusieurs entreprises. Ce registre est
la SEULE autorite qui dit qu'une entreprise existe, ou vont ses ecritures
et si elle a le droit d'etre traitee. Rien d'autre - ni un sujet d'email,
ni un nom lu dans un document, ni une correspondance approximative - ne
peut faire entrer une piece dans une comptabilite.

Trois regles portent toute la securite du multi-tenant :

  1. Une entreprise n'est JAMAIS creee depuis un email entrant. L'ajout
     passe par une action administrateur explicite. Un inconnu qui ecrit
     a une adresse de l'agent ne peut donc pas se fabriquer un tenant.

  2. Seule une entreprise ACTIVE peut recevoir une ecriture comptable.
     PENDING_CONFIGURATION, SUSPENDED et DISABLED acceptent la reception
     et la tracabilite, jamais l'ecriture.

  3. AUCUN secret n'entre ici. Le registre voyage dans les journaux, les
     rapports et les sauvegardes ; il ne contient que des identifiants et
     de la configuration comptable.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

# --- etats ----------------------------------------------------------------

PENDING_CONFIGURATION = "PENDING_CONFIGURATION"
ACTIVE = "ACTIVE"
SUSPENDED = "SUSPENDED"
DISABLED = "DISABLED"

STATUSES = frozenset({PENDING_CONFIGURATION, ACTIVE, SUSPENDED, DISABLED})

# Seul cet etat autorise une ecriture comptable. Les autres laissent
# passer la reception et la trace, jamais la comptabilisation : une
# societe a moitie configuree qui ecrirait dans un classeur incomplet
# produirait une comptabilite fausse, plus difficile a reparer qu'un
# email en attente.
WRITABLE_STATUSES = frozenset({ACTIVE})

# --- validation de la configuration --------------------------------------

CONFIG_OK = "OK"
CONFIG_INCOMPLETE = "INCOMPLETE"
CONFIG_UNCHECKED = "UNCHECKED"

TEMPLATE_VERSION_UNSET = ""


class CompanyError(RuntimeError):
    """Registre incoherent, ou operation refusee par une regle du registre."""


_COMPANY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def normalize_company_id(value: str) -> str:
    """Identifiant immuable, en minuscules, sans espace ni accent.

    L'identifiant sert de cle etrangere dans toutes les tables d'etat et
    de prefixe dans les journaux. Le figer dans une forme etroite evite
    qu'une majuscule ou une espace de trop cree un second tenant fantome
    portant le meme nom.
    """
    candidat = (value or "").strip().lower()
    if not _COMPANY_ID_RE.match(candidat):
        raise CompanyError(
            f"identifiant d'entreprise invalide : '{value}' "
            "(attendu : minuscules, chiffres et tirets, 2 a 63 caracteres)"
        )
    return candidat


def normalize_alias(value: str) -> str:
    """Adresse de reception normalisee pour comparaison stricte.

    On compare des adresses, jamais des noms. La casse et les espaces
    sont neutralises ; le reste est conserve tel quel, y compris le
    sous-adressage `+etiquette`, qui est precisement ce qui distingue
    deux tenants sur une meme boite.
    """
    return (value or "").strip().lower()


def normalize_aliases(values: Iterable[str]) -> tuple[str, ...]:
    vues: list[str] = []
    for brut in values or ():
        alias = normalize_alias(brut)
        if alias and alias not in vues:
            vues.append(alias)
    return tuple(vues)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    company_id TEXT PRIMARY KEY,
    legal_name TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    inbound_aliases TEXT NOT NULL DEFAULT '[]',
    allowed_admin_senders TEXT NOT NULL DEFAULT '[]',
    sheet_id TEXT NOT NULL DEFAULT '',
    drive_folder_id TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT '',
    allowed_vat_rates TEXT NOT NULL DEFAULT '[]',
    telegram_chat_id TEXT NOT NULL DEFAULT '',
    template_version TEXT NOT NULL DEFAULT '',
    config_validation_status TEXT NOT NULL DEFAULT 'UNCHECKED',
    created_at TEXT NOT NULL,
    activated_at TEXT,
    last_successful_cycle TEXT
);

-- Un alias ne peut appartenir qu'a UNE entreprise. La contrainte est
-- portee par la base, pas par le code appelant : c'est elle qui rend
-- structurellement impossible qu'une meme adresse alimente deux
-- comptabilites.
CREATE TABLE IF NOT EXISTS company_aliases (
    alias TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_company_aliases_company
    ON company_aliases(company_id);
"""


@dataclass(frozen=True)
class Company:
    """Une entreprise du registre. Aucun secret, par construction."""

    company_id: str
    status: str
    legal_name: str = ""
    display_name: str = ""
    inbound_aliases: tuple[str, ...] = ()
    allowed_admin_senders: tuple[str, ...] = ()
    sheet_id: str = ""
    drive_folder_id: str = ""
    country: str = ""
    currency: str = ""
    allowed_vat_rates: tuple[Decimal, ...] = ()
    telegram_chat_id: str = ""
    template_version: str = TEMPLATE_VERSION_UNSET
    config_validation_status: str = CONFIG_UNCHECKED
    created_at: str = ""
    activated_at: str | None = None
    last_successful_cycle: str | None = None

    @property
    def can_write(self) -> bool:
        """Le droit d'ecrire une ecriture comptable.

        Il ne suffit pas d'etre ACTIVE : sans classeur ni dossier Drive,
        une entreprise « active » ecrirait dans le vide.
        """
        return (
            self.status in WRITABLE_STATUSES
            and bool(self.sheet_id)
            and bool(self.drive_folder_id)
        )

    @property
    def missing_for_activation(self) -> tuple[str, ...]:
        """Champs qui manquent encore pour activer l'entreprise.

        Sert a poser UNE question groupee a l'exploitant plutot que de
        decouvrir les trous un par un, et a expliquer un bootstrap reste
        en PENDING_CONFIGURATION.
        """
        manquants: list[str] = []
        if not self.legal_name.strip():
            manquants.append("legal_name")
        if not self.inbound_aliases:
            manquants.append("inbound_aliases")
        if not self.sheet_id:
            manquants.append("sheet_id")
        if not self.drive_folder_id:
            manquants.append("drive_folder_id")
        if not self.country.strip():
            manquants.append("country")
        if not self.currency.strip():
            manquants.append("currency")
        if not self.allowed_vat_rates:
            manquants.append("allowed_vat_rates")
        if not str(self.telegram_chat_id).strip():
            manquants.append("telegram_chat_id")
        return tuple(manquants)


def _rates_to_json(rates: Iterable[Decimal | str | int | float]) -> str:
    valeurs: list[str] = []
    for brut in rates or ():
        try:
            valeurs.append(str(Decimal(str(brut))))
        except (InvalidOperation, ValueError) as exc:
            raise CompanyError(f"taux de TVA invalide : {brut!r}") from exc
    return json.dumps(valeurs)


def _rates_from_json(payload: str) -> tuple[Decimal, ...]:
    try:
        brut = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return ()
    sortie: list[Decimal] = []
    for valeur in brut:
        try:
            sortie.append(Decimal(str(valeur)))
        except (InvalidOperation, ValueError):
            continue
    return tuple(sortie)


def _list_from_json(payload: str) -> tuple[str, ...]:
    try:
        brut = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return ()
    return tuple(str(v) for v in brut if str(v).strip())


def ensure_schema(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _row_to_company(row: sqlite3.Row) -> Company:
    return Company(
        company_id=row["company_id"],
        status=row["status"],
        legal_name=row["legal_name"] or "",
        display_name=row["display_name"] or "",
        inbound_aliases=_list_from_json(row["inbound_aliases"]),
        allowed_admin_senders=_list_from_json(row["allowed_admin_senders"]),
        sheet_id=row["sheet_id"] or "",
        drive_folder_id=row["drive_folder_id"] or "",
        country=row["country"] or "",
        currency=row["currency"] or "",
        allowed_vat_rates=_rates_from_json(row["allowed_vat_rates"]),
        telegram_chat_id=row["telegram_chat_id"] or "",
        template_version=row["template_version"] or "",
        config_validation_status=row["config_validation_status"] or CONFIG_UNCHECKED,
        created_at=row["created_at"] or "",
        activated_at=row["activated_at"],
        last_successful_cycle=row["last_successful_cycle"],
    )


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def register_company(
    db_path: str,
    company_id: str,
    *,
    display_name: str = "",
    legal_name: str = "",
    status: str = PENDING_CONFIGURATION,
    inbound_aliases: Iterable[str] = (),
    allowed_admin_senders: Iterable[str] = (),
    sheet_id: str = "",
    drive_folder_id: str = "",
    country: str = "",
    currency: str = "",
    allowed_vat_rates: Iterable[Decimal | str] = (),
    telegram_chat_id: str = "",
    template_version: str = TEMPLATE_VERSION_UNSET,
) -> Company:
    """Inscrit une entreprise. ACTION ADMINISTRATEUR UNIQUEMENT.

    Aucun chemin partant d'un email entrant n'appelle cette fonction :
    c'est la garantie qu'une societe inconnue ne peut pas naitre d'un
    message externe.
    """
    identifiant = normalize_company_id(company_id)
    if status not in STATUSES:
        raise CompanyError(f"statut inconnu : '{status}'")
    alias_normalises = normalize_aliases(inbound_aliases)

    with _connect(db_path) as conn:
        deja = conn.execute(
            "SELECT company_id FROM companies WHERE company_id = ?", (identifiant,)
        ).fetchone()
        if deja is not None:
            raise CompanyError(f"l'entreprise '{identifiant}' existe deja")

        # Un alias deja pris appartient a une autre comptabilite : le
        # refus est total, on ne « vole » pas une adresse en silence.
        for alias in alias_normalises:
            proprietaire = conn.execute(
                "SELECT company_id FROM company_aliases WHERE alias = ?", (alias,)
            ).fetchone()
            if proprietaire is not None:
                raise CompanyError(
                    f"l'alias '{alias}' appartient deja a "
                    f"'{proprietaire['company_id']}'"
                )

        maintenant = _now()
        conn.execute(
            "INSERT INTO companies (company_id, legal_name, display_name, status,"
            " inbound_aliases, allowed_admin_senders, sheet_id, drive_folder_id,"
            " country, currency, allowed_vat_rates, telegram_chat_id,"
            " template_version, config_validation_status, created_at,"
            " activated_at, last_successful_cycle)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identifiant,
                legal_name.strip(),
                (display_name or legal_name or identifiant).strip(),
                status,
                json.dumps(list(alias_normalises)),
                json.dumps([normalize_alias(a) for a in allowed_admin_senders or ()]),
                sheet_id.strip(),
                drive_folder_id.strip(),
                country.strip(),
                currency.strip().upper(),
                _rates_to_json(allowed_vat_rates),
                str(telegram_chat_id).strip(),
                template_version,
                CONFIG_UNCHECKED,
                maintenant,
                maintenant if status == ACTIVE else None,
                None,
            ),
        )
        for alias in alias_normalises:
            conn.execute(
                "INSERT INTO company_aliases (alias, company_id, created_at)"
                " VALUES (?,?,?)",
                (alias, identifiant, maintenant),
            )
        conn.commit()
    entreprise = get_company(db_path, identifiant)
    assert entreprise is not None
    return entreprise


def get_company(db_path: str, company_id: str) -> Company | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM companies WHERE company_id = ?",
            (normalize_alias(company_id),),
        ).fetchone()
    return _row_to_company(row) if row is not None else None


def list_companies(db_path: str) -> tuple[Company, ...]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM companies ORDER BY company_id").fetchall()
    return tuple(_row_to_company(row) for row in rows)


def company_for_alias(db_path: str, alias: str) -> Company | None:
    """Entreprise proprietaire d'une adresse de reception, ou None.

    Comparaison STRICTE sur l'adresse normalisee. Aucune approximation,
    aucun rapprochement de nom : c'est le coeur du routage.
    """
    cible = normalize_alias(alias)
    if not cible:
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT company_id FROM company_aliases WHERE alias = ?", (cible,)
        ).fetchone()
    return get_company(db_path, row["company_id"]) if row is not None else None


def set_status(db_path: str, company_id: str, status: str) -> Company:
    if status not in STATUSES:
        raise CompanyError(f"statut inconnu : '{status}'")
    identifiant = normalize_company_id(company_id)
    with _connect(db_path) as conn:
        courant = conn.execute(
            "SELECT status, activated_at FROM companies WHERE company_id = ?",
            (identifiant,),
        ).fetchone()
        if courant is None:
            raise CompanyError(f"entreprise inconnue : '{identifiant}'")
        # `activated_at` marque la PREMIERE activation et ne bouge plus :
        # c'est une date d'audit, pas un horodatage de derniere reprise.
        activated = courant["activated_at"]
        if status == ACTIVE and not activated:
            activated = _now()
        conn.execute(
            "UPDATE companies SET status = ?, activated_at = ? WHERE company_id = ?",
            (status, activated, identifiant),
        )
        conn.commit()
    entreprise = get_company(db_path, identifiant)
    assert entreprise is not None
    return entreprise


_UPDATABLE = {
    "legal_name", "display_name", "sheet_id", "drive_folder_id", "country",
    "currency", "telegram_chat_id", "template_version",
    "config_validation_status", "last_successful_cycle",
}


def update_company(db_path: str, company_id: str, **champs: Any) -> Company:
    """Met a jour la configuration. Le statut passe par `set_status`.

    Les taux de TVA et les alias ont leur propre chemin : les premiers
    parce qu'ils se serialisent, les seconds parce qu'ils engagent la
    table d'unicite.
    """
    identifiant = normalize_company_id(company_id)
    taux = champs.pop("allowed_vat_rates", None)
    inconnus = set(champs) - _UPDATABLE
    if inconnus:
        raise CompanyError(f"champs non modifiables : {sorted(inconnus)}")

    with _connect(db_path) as conn:
        if conn.execute(
            "SELECT 1 FROM companies WHERE company_id = ?", (identifiant,)
        ).fetchone() is None:
            raise CompanyError(f"entreprise inconnue : '{identifiant}'")
        for colonne, valeur in champs.items():
            conn.execute(
                f"UPDATE companies SET {colonne} = ? WHERE company_id = ?",
                (str(valeur).strip(), identifiant),
            )
        if taux is not None:
            conn.execute(
                "UPDATE companies SET allowed_vat_rates = ? WHERE company_id = ?",
                (_rates_to_json(taux), identifiant),
            )
        conn.commit()
    entreprise = get_company(db_path, identifiant)
    assert entreprise is not None
    return entreprise


def add_alias(db_path: str, company_id: str, alias: str) -> Company:
    """Rattache une adresse de reception a une entreprise.

    Refuse si l'adresse appartient deja a une autre : deux comptabilites
    ne peuvent jamais se disputer une meme boite.
    """
    identifiant = normalize_company_id(company_id)
    cible = normalize_alias(alias)
    if not cible:
        raise CompanyError("alias vide")
    with _connect(db_path) as conn:
        if conn.execute(
            "SELECT 1 FROM companies WHERE company_id = ?", (identifiant,)
        ).fetchone() is None:
            raise CompanyError(f"entreprise inconnue : '{identifiant}'")
        proprietaire = conn.execute(
            "SELECT company_id FROM company_aliases WHERE alias = ?", (cible,)
        ).fetchone()
        if proprietaire is not None and proprietaire["company_id"] != identifiant:
            raise CompanyError(
                f"l'alias '{cible}' appartient deja a '{proprietaire['company_id']}'"
            )
        if proprietaire is None:
            conn.execute(
                "INSERT INTO company_aliases (alias, company_id, created_at)"
                " VALUES (?,?,?)",
                (cible, identifiant, _now()),
            )
        row = conn.execute(
            "SELECT inbound_aliases FROM companies WHERE company_id = ?",
            (identifiant,),
        ).fetchone()
        courants = list(_list_from_json(row["inbound_aliases"]))
        if cible not in courants:
            courants.append(cible)
            conn.execute(
                "UPDATE companies SET inbound_aliases = ? WHERE company_id = ?",
                (json.dumps(courants), identifiant),
            )
        conn.commit()
    entreprise = get_company(db_path, identifiant)
    assert entreprise is not None
    return entreprise


def mark_cycle_success(db_path: str, company_id: str) -> None:
    """Horodate le dernier cycle reussi. Sert a la supervision.

    Une entreprise ACTIVE dont ce champ ne bouge plus signale une panne
    silencieuse - un quota, une connexion morte - que le simple etat
    ACTIVE ne montrerait pas.
    """
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE companies SET last_successful_cycle = ? WHERE company_id = ?",
            (_now(), normalize_company_id(company_id)),
        )
        conn.commit()
