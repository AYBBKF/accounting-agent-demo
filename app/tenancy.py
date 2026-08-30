"""Passage du mono-entreprise au multi-entreprises de l'etat persistant.

L'agent a longtemps servi une seule comptabilite : ses tables d'etat ne
portent donc aucune notion d'entreprise. Toute ligne existante appartient
en realite a XBLASTE. Ce module ajoute `company_id` partout, rattache
l'historique a XBLASTE, et scope par entreprise toutes les recherches qui
decident d'une ecriture comptable.

Trois exigences gouvernent l'ecriture de cette migration :

  * IDEMPOTENTE - le conteneur redemarre, la migration se rejoue. Elle
    doit pouvoir tourner cent fois sans rien changer apres la premiere.
  * RECUPERABLE - deux tables doivent changer de cle primaire, ce que
    SQLite ne sait pas faire en place. Leur version d'origine est
    CONSERVEE sous un nom suffixe `_legacy_v1`, jamais supprimee : c'est
    le filet de securite d'un retour arriere.
  * SANS EFFET DE BORD METIER - aucun compteur Sheets ne bouge, aucun
    curseur Gmail ne recule, aucune notification n'est reemise et aucune
    relecture LLM n'est declenchee. La migration deplace de la structure,
    pas de la comptabilite.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Identifiant de l'entreprise historique. Toute donnee anterieure au
# multi-tenant lui appartient : c'est la seule comptabilite qui existait.
LEGACY_COMPANY_ID = "xblaste"

# Tables ou `company_id` s'ajoute simplement en colonne.
_TABLES_A_COLONNE = (
    "documents",
)

# Tables dont la CLE PRIMAIRE doit inclure `company_id`. SQLite ne sait
# pas modifier une cle primaire en place : on reconstruit, et on garde
# l'ancienne table sous un autre nom plutot que de la detruire.
# `bank_line_fingerprints` et `calendar_events` en font partie pour une
# raison decouverte en test : leur cle primaire etait GLOBALE. Deux
# societes qui ont un compte dans la meme banque produisent la meme
# empreinte pour le meme type de mouvement ; la seconde etait alors
# rejetee en silence et sa ligne bancaire disparaissait.
_TABLES_A_RECONSTRUIRE = (
    "email_notifications",
    "gmail_sync_state",
    "bank_line_fingerprints",
    "calendar_events",
)

_SUFFIXE_LEGACY = "_legacy_v1"

_NOUVELLES_TABLES = {
    "email_notifications": """
        CREATE TABLE email_notifications (
            company_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            gmail_message_id TEXT NOT NULL,
            signature TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (company_id, chat_id, gmail_message_id)
        )
    """,
    "gmail_sync_state": """
        CREATE TABLE gmail_sync_state (
            company_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            history_id TEXT,
            last_internal_date INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (company_id, chat_id)
        )
    """,
    "bank_line_fingerprints": """
        CREATE TABLE bank_line_fingerprints (
            company_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            row_index INTEGER,
            doc_key TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (company_id, fingerprint)
        )
    """,
    "calendar_events": """
        CREATE TABLE calendar_events (
            company_id TEXT NOT NULL,
            event_key TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            event_id TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (company_id, event_key)
        )
    """,
}

# Index scopes par entreprise. Ce sont eux qui rendent structurellement
# impossible qu'une recherche de doublon traverse la frontiere d'un
# tenant : la meme facture envoyee a deux societes doit produire une
# ecriture dans chacune.
_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_documents_company_state"
    " ON documents(company_id, chat_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_documents_company_sha"
    " ON documents(company_id, file_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_documents_company_message"
    " ON documents(company_id, gmail_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_documents_company_business"
    " ON documents(company_id, doc_type, numero)",
)


@dataclass
class MigrationReport:
    """Ce que la migration a REELLEMENT fait, table par table.

    Sert de preuve : une migration qui ne rapporte rien ne se distingue
    pas d'une migration qui n'a pas tourne.
    """

    already_migrated: bool = False
    columns_added: tuple[str, ...] = ()
    tables_rebuilt: tuple[str, ...] = ()
    rows_backfilled: dict[str, int] = field(default_factory=dict)
    legacy_tables_kept: tuple[str, ...] = ()
    company_id: str = LEGACY_COMPANY_ID

    @property
    def total_rows(self) -> int:
        return sum(self.rows_backfilled.values())


def _colonnes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_existe(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def is_migrated(db_path: str) -> bool:
    """La base porte-t-elle deja la structure multi-tenant ?"""
    with sqlite3.connect(db_path) as conn:
        for table in _TABLES_A_COLONNE + _TABLES_A_RECONSTRUIRE:
            if not _table_existe(conn, table):
                continue
            if "company_id" not in _colonnes(conn, table):
                return False
    return True


def migrate_to_multi_tenant(
    db_path: str, *, company_id: str = LEGACY_COMPANY_ID
) -> MigrationReport:
    """Rattache tout l'historique a une entreprise et scope les etats.

    Rejouable sans effet : les colonnes deja presentes ne sont pas
    recreees, les lignes deja rattachees ne sont pas retouchees.
    """
    rapport = MigrationReport(company_id=company_id)
    ajoutees: list[str] = []
    reconstruites: list[str] = []
    conservees: list[str] = []

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        # Une seule transaction : soit la base est entierement migree,
        # soit elle reste exactement dans son etat d'origine. Une
        # migration a moitie faite serait le pire des deux mondes.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in _TABLES_A_COLONNE:
                if not _table_existe(conn, table):
                    continue
                if "company_id" not in _colonnes(conn, table):
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN company_id TEXT"
                        " NOT NULL DEFAULT ''"
                    )
                    ajoutees.append(table)

            for table in _TABLES_A_RECONSTRUIRE:
                if not _table_existe(conn, table):
                    conn.execute(_NOUVELLES_TABLES[table])
                    reconstruites.append(table)
                    continue
                if "company_id" in _colonnes(conn, table):
                    continue

                sauvegarde = f"{table}{_SUFFIXE_LEGACY}"
                # L'ancienne table est CONSERVEE, jamais supprimee : elle
                # porte l'etat exact d'avant migration, seul recours si
                # un retour arriere devenait necessaire.
                if _table_existe(conn, sauvegarde):
                    raise RuntimeError(
                        f"la sauvegarde '{sauvegarde}' existe deja : "
                        "migration precedente interrompue, intervention requise"
                    )
                anciennes = list(_colonnes(conn, table))
                conn.execute(f"ALTER TABLE {table} RENAME TO {sauvegarde}")
                conn.execute(_NOUVELLES_TABLES[table])
                communes = [c for c in anciennes if c != "company_id"]
                liste = ", ".join(communes)
                conn.execute(
                    f"INSERT INTO {table} (company_id, {liste})"
                    f" SELECT ?, {liste} FROM {sauvegarde}",
                    (company_id,),
                )
                reconstruites.append(table)
                conservees.append(sauvegarde)

            # Rattachement de l'historique. `company_id = ''` identifie
            # exactement les lignes anterieures au multi-tenant.
            for table in _TABLES_A_COLONNE + _TABLES_A_RECONSTRUIRE:
                if not _table_existe(conn, table):
                    continue
                curseur = conn.execute(
                    f"UPDATE {table} SET company_id = ? WHERE company_id = '' "
                    "OR company_id IS NULL",
                    (company_id,),
                )
                if curseur.rowcount:
                    rapport.rows_backfilled[table] = curseur.rowcount

            for requete in _INDEX:
                conn.execute(requete)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    rapport.columns_added = tuple(ajoutees)
    rapport.tables_rebuilt = tuple(reconstruites)
    rapport.legacy_tables_kept = tuple(conservees)
    rapport.already_migrated = not (ajoutees or reconstruites or rapport.rows_backfilled)
    return rapport


def company_counts(db_path: str) -> dict[str, dict[str, int]]:
    """Nombre de lignes par table ET par entreprise.

    C'est la preuve de non-contamination : apres traitement, chaque
    entreprise ne doit avoir bouge que sur ses propres lignes.
    """
    sortie: dict[str, dict[str, int]] = {}
    with sqlite3.connect(db_path) as conn:
        for table in _TABLES_A_COLONNE + _TABLES_A_RECONSTRUIRE:
            if not _table_existe(conn, table):
                continue
            if "company_id" not in _colonnes(conn, table):
                continue
            par_entreprise: dict[str, int] = {}
            for identifiant, total in conn.execute(
                f"SELECT company_id, COUNT(*) FROM {table} GROUP BY company_id"
            ):
                par_entreprise[identifiant or ""] = int(total)
            sortie[table] = par_entreprise
    return sortie


def orphan_rows(db_path: str) -> dict[str, int]:
    """Lignes qui n'appartiennent a aucune entreprise.

    Doit rester vide apres migration : une ligne orpheline est une
    ecriture qu'aucune comptabilite ne revendique.
    """
    orphelines: dict[str, int] = {}
    with sqlite3.connect(db_path) as conn:
        for table in _TABLES_A_COLONNE + _TABLES_A_RECONSTRUIRE:
            if not _table_existe(conn, table):
                continue
            if "company_id" not in _colonnes(conn, table):
                continue
            total = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE company_id = '' "
                "OR company_id IS NULL"
            ).fetchone()[0]
            if total:
                orphelines[table] = int(total)
    return orphelines
