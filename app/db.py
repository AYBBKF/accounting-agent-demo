"""Acces SQLite minimal pour la demo (pas de Postgres/Redis).

Toutes les colonnes monetaires sont stockees en TEXT et manipulees
exclusivement via Decimal cote application (jamais de float).

SQLite reste la base interne principale du bot. Google Sheets (optionnel)
n'est qu'une vue synchronisee : les ID stables utilises pour la
synchronisation idempotente (colonne A des onglets) sont derives des ID
auto-incrementes SQLite (ex. "INV-<chat_id>-<id>").
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL DEFAULT '',
    fournisseur TEXT NOT NULL,
    numero TEXT NOT NULL,
    date_facture TEXT NOT NULL,
    montant_ht TEXT NOT NULL,
    taux_tva TEXT NOT NULL,
    montant_tva TEXT NOT NULL,
    montant_ttc TEXT NOT NULL,
    categorie TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS demo_bank_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL DEFAULT '',
    date_operation TEXT NOT NULL,
    libelle TEXT NOT NULL,
    montant TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gmail_processed_emails (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT,
    chat_id TEXT NOT NULL,
    subject TEXT,
    sender TEXT,
    received_at TEXT,
    attachment_name TEXT,
    numero TEXT,
    status TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS invoice_fingerprints (
    fingerprint TEXT PRIMARY KEY,
    stable_id TEXT,
    numero TEXT,
    ice TEXT,
    message_id TEXT,
    tab TEXT,
    row_index INTEGER,
    lines_written INTEGER NOT NULL DEFAULT 0,
    drive_link TEXT,
    log_row INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gmail_cursor (
    chat_id TEXT PRIMARY KEY,
    since_epoch INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS demo_reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL DEFAULT '',
    invoice_numero TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Migrations legeres pour les bases crees avant l'ajout de chat_id.
_MIGRATIONS = [
    "ALTER TABLE demo_invoices ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE demo_bank_lines ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''",
    # Points de reprise de l'import (bases creees avant l'import idempotent).
    "ALTER TABLE invoice_fingerprints ADD COLUMN tab TEXT",
    "ALTER TABLE invoice_fingerprints ADD COLUMN row_index INTEGER",
    "ALTER TABLE invoice_fingerprints ADD COLUMN lines_written INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE invoice_fingerprints ADD COLUMN drive_link TEXT",
    "ALTER TABLE invoice_fingerprints ADD COLUMN log_row INTEGER",
]


def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        for statement in _MIGRATIONS:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # colonne deja presente
        conn.commit()


@contextmanager
def connect(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def save_invoices(db_path: str, chat_id: int, invoices: Iterable[Any]) -> list[int]:
    """Persiste les factures generees (remplace celles de la session en cours
    pour ce chat) et retourne les ID SQLite dans le meme ordre.

    Les montants TVA/TTC sont recalcules via `simulate_vat` (jamais stockes
    tels quels par le generateur de demo, qui ne fournit que HT + taux)."""
    from app.vat import simulate_vat

    ids: list[int] = []
    with connect(db_path) as conn:
        conn.execute("DELETE FROM demo_invoices WHERE chat_id = ?", (str(chat_id),))
        for inv in invoices:
            vat = simulate_vat(inv.montant_ht, inv.taux_tva)
            cur = conn.execute(
                "INSERT INTO demo_invoices "
                "(chat_id, fournisseur, numero, date_facture, montant_ht, taux_tva, "
                "montant_tva, montant_ttc, categorie) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(chat_id),
                    inv.fournisseur,
                    inv.numero,
                    inv.date_facture.isoformat(),
                    str(vat.montant_ht),
                    str(vat.taux_tva),
                    str(vat.montant_tva),
                    str(vat.montant_ttc),
                    "",
                ),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
    return ids


def save_bank_lines(db_path: str, chat_id: int, bank_lines: Iterable[Any]) -> list[int]:
    ids: list[int] = []
    with connect(db_path) as conn:
        conn.execute("DELETE FROM demo_bank_lines WHERE chat_id = ?", (str(chat_id),))
        for line in bank_lines:
            cur = conn.execute(
                "INSERT INTO demo_bank_lines (chat_id, date_operation, libelle, montant) "
                "VALUES (?, ?, ?, ?)",
                (
                    str(chat_id),
                    line.date_operation.isoformat(),
                    line.libelle,
                    str(line.montant),
                ),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
    return ids


def save_reconciliations(db_path: str, chat_id: int, results: Iterable[Any]) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM demo_reconciliations WHERE chat_id = ?", (str(chat_id),))
        for r in results:
            conn.execute(
                "INSERT INTO demo_reconciliations (chat_id, invoice_numero, status, detail) "
                "VALUES (?, ?, ?, ?)",
                (str(chat_id), r.invoice.numero, r.status, r.detail),
            )
        conn.commit()


def stable_invoice_id(chat_id: int, db_id: int) -> str:
    return f"INV-{chat_id}-{db_id}"


def stable_bank_line_id(chat_id: int, db_id: int) -> str:
    return f"BANK-{chat_id}-{db_id}"


# --- Emails Gmail traites (anti-doublon du watcher) -----------------------
# La cle primaire est le message_id Gmail : un email deja vu ne peut jamais
# etre retraite, meme apres un redemarrage du conteneur.

def claim_gmail_message(
    db_path: str,
    message_id: str,
    chat_id: int,
    *,
    thread_id: str = "",
    subject: str = "",
    sender: str = "",
    received_at: str = "",
    attachment_name: str = "",
    numero: str = "",
    payload: str = "",
    status: str = "pending",
) -> bool:
    """Enregistre un email comme pris en charge.

    Retourne True si l'email est nouveau (il vient d'etre reserve), False
    s'il avait deja ete traite - dans ce cas l'appelant doit l'ignorer.
    L'insertion est atomique : deux passages concurrents ne peuvent pas
    reserver le meme message_id.
    """
    from datetime import datetime, timezone

    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO gmail_processed_emails "
            "(message_id, thread_id, chat_id, subject, sender, received_at, "
            " attachment_name, numero, status, payload, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                message_id, thread_id, str(chat_id), subject, sender, received_at,
                attachment_name, numero, status, payload,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return cur.rowcount == 1


def get_gmail_message(db_path: str, message_id: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM gmail_processed_emails WHERE message_id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None


def set_gmail_message_status(db_path: str, message_id: str, status: str) -> None:
    from datetime import datetime, timezone

    with connect(db_path) as conn:
        conn.execute(
            "UPDATE gmail_processed_emails SET status = ?, decided_at = ? WHERE message_id = ?",
            (status, datetime.now(timezone.utc).isoformat(timespec="seconds"), message_id),
        )
        conn.commit()


# --- Empreintes de factures (anti-doublon metier) -------------------------
# Cle : (ICE fournisseur + numero de facture). Le message_id Gmail ne suffit
# pas : la meme facture peut arriver deux fois par deux emails differents
# (renvoi, transfert), et ce serait alors une double ecriture comptable.

def claim_invoice_fingerprint(
    db_path: str,
    fingerprint: str,
    *,
    stable_id: str = "",
    numero: str = "",
    ice: str = "",
    message_id: str = "",
) -> bool:
    """Reserve une empreinte. True si elle est nouvelle, False si doublon.

    Une empreinte vide (ICE ou numero manquant) n'est jamais reservee : sans
    ICE, aucun doublon ne peut etre affirme avec certitude.
    """
    if not fingerprint:
        return True
    from datetime import datetime, timezone

    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO invoice_fingerprints "
            "(fingerprint, stable_id, numero, ice, message_id, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                fingerprint, stable_id, numero, ice, message_id,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return cur.rowcount == 1


def get_invoice_fingerprint(db_path: str, fingerprint: str) -> dict[str, Any] | None:
    if not fingerprint:
        return None
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM invoice_fingerprints WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return dict(row) if row else None


def release_invoice_fingerprint(db_path: str, fingerprint: str) -> None:
    """Libere une empreinte reservee dont l'ecriture a finalement echoue."""
    if not fingerprint:
        return
    with connect(db_path) as conn:
        conn.execute("DELETE FROM invoice_fingerprints WHERE fingerprint = ?", (fingerprint,))
        conn.commit()


def update_invoice_fingerprint(db_path: str, fingerprint: str, **fields: Any) -> None:
    """Met a jour les points de reprise d'un import (onglet, ligne, Drive...).

    Chaque etape reussie est enregistree immediatement : une relance apres
    panne reprend exactement la ou l'import s'etait arrete, sans jamais
    reecrire une ligne deja ecrite.
    """
    if not fingerprint or not fields:
        return
    allowed = ("stable_id", "tab", "row_index", "lines_written", "drive_link", "log_row")
    columns = [k for k in fields if k in allowed]
    if not columns:
        return
    assignments = ", ".join(f"{c} = ?" for c in columns)
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE invoice_fingerprints SET {assignments} WHERE fingerprint = ?",
            (*(fields[c] for c in columns), fingerprint),
        )
        conn.commit()


def list_partial_imports(db_path: str, chat_id: int) -> list[dict[str, Any]]:
    """Emails dont l'ecriture comptable a reussi mais dont l'archivage ou le
    journal n'a pas abouti : a terminer au prochain cycle."""
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM gmail_processed_emails WHERE chat_id = ? AND status = 'partial' "
            "ORDER BY created_at",
            (str(chat_id),),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Curseur Gmail durable -----------------------------------------------
# Fixe au PREMIER demarrage : le worker n'importera jamais un email anterieur.
# Sans lui, elargir la requete a toute la boite de reception ferait remonter
# des annees d'anciennes pieces jointes.

def get_or_init_gmail_cursor(db_path: str, chat_id: int, now_epoch: int) -> int:
    """Retourne le curseur (epoch en secondes), en le creant au besoin.

    Le curseur n'est JAMAIS avance ni recule ensuite : c'est un plancher
    stable. L'anti-doublon par message_id se charge du reste.
    """
    from datetime import datetime, timezone

    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO gmail_cursor (chat_id, since_epoch, created_at) "
            "VALUES (?,?,?)",
            (
                str(chat_id), int(now_epoch),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT since_epoch FROM gmail_cursor WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        return int(row[0])
