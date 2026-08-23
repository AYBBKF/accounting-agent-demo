"""Etat persistant du pipeline documentaire.

Trois roles :

  1. une MACHINE D'ETAT par document, pour qu'une panne au milieu du
     traitement reprenne exactement a l'etape manquante ;
  2. l'anti-doublon a trois niveaux : cle d'idempotence (client + email +
     piece jointe + empreinte du fichier), empreinte SHA-256 du fichier
     seul (le meme PDF renvoye dans un autre email), et empreinte metier
     (tiers + numero) ;
  3. le CURSEUR Gmail durable, qui avance sans jamais perdre d'email.

Toutes les tables sont cloisonnees par `chat_id` : un client ne peut ni
voir ni modifier les documents d'un autre.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db import connect

# --- machine d'etat -------------------------------------------------------
DETECTED = "detected"
DOWNLOADED = "downloaded"
EXTRACTED = "extracted"
VALIDATED = "validated"
SHEET_WRITTEN = "sheet_written"
DETAILS_WRITTEN = "details_written"
DRIVE_ARCHIVED = "drive_archived"
CALENDAR_CREATED = "calendar_created"
LOGGED = "logged"
COMPLETED = "completed"
NEEDS_REVIEW = "needs_review"
SKIPPED = "skipped"
DUPLICATE = "duplicate"
PARTIAL = "partial"
FAILED = "failed"

ALL_STATES = (
    DETECTED, DOWNLOADED, EXTRACTED, VALIDATED, SHEET_WRITTEN, DETAILS_WRITTEN,
    DRIVE_ARCHIVED, CALENDAR_CREATED, LOGGED, COMPLETED, NEEDS_REVIEW, SKIPPED,
    DUPLICATE, PARTIAL, FAILED,
)

# Etats a partir desquels la ligne comptable EXISTE deja : on ne la reecrit
# jamais, on ne fait que terminer les etapes suivantes.
STATES_AFTER_SHEET = frozenset({
    SHEET_WRITTEN, DETAILS_WRITTEN, DRIVE_ARCHIVED, CALENDAR_CREATED, LOGGED,
    PARTIAL,
})
TERMINAL_STATES = frozenset({COMPLETED, SKIPPED, DUPLICATE})

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_key TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    gmail_message_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    filename TEXT,
    container TEXT,
    parent_attachment_id TEXT,
    parent_filename TEXT,
    member_path TEXT,
    local_path TEXT,
    doc_type TEXT,
    numero TEXT,
    state TEXT NOT NULL,
    stable_id TEXT,
    tab TEXT,
    row_index INTEGER,
    lines_written INTEGER NOT NULL DEFAULT 0,
    drive_link TEXT,
    calendar_event TEXT,
    log_row INTEGER,
    payload TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_state ON documents(chat_id, state);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(chat_id, file_sha256);
CREATE INDEX IF NOT EXISTS idx_documents_message ON documents(chat_id, gmail_message_id);

CREATE TABLE IF NOT EXISTS bank_line_fingerprints (
    fingerprint TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    row_index INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_events (
    event_key TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    event_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gmail_sync_state (
    chat_id TEXT PRIMARY KEY,
    history_id TEXT,
    last_internal_date INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Colonnes ajoutees apres la mise en production. `CREATE TABLE IF NOT EXISTS`
# ne les ajoute PAS a une table deja creee : sans cette migration, un volume
# existant garderait l'ancienne table et toute ecriture echouerait.
_ADDED_COLUMNS = (
    ("parent_attachment_id", "TEXT"),
    ("parent_filename", "TEXT"),
    ("member_path", "TEXT"),
    ("local_path", "TEXT"),
    ("review_archive", "INTEGER DEFAULT 0"),
)


def ensure_schema(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        for column, kind in _ADDED_COLUMNS:
            if column not in existing:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {column} {kind}")
        conn.commit()


# --- documents ------------------------------------------------------------

def claim_document(
    db_path: str,
    doc_key: str,
    chat_id: int,
    *,
    gmail_message_id: str,
    attachment_id: str,
    file_sha256: str,
    filename: str = "",
    container: str = "",
    parent_attachment_id: str = "",
    parent_filename: str = "",
    member_path: str = "",
    local_path: str = "",
) -> bool:
    """Reserve un document. True s'il est nouveau, False s'il est deja connu.

    L'insertion est atomique : deux cycles concurrents ne peuvent pas
    reserver le meme document.
    """
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO documents "
            "(doc_key, chat_id, gmail_message_id, attachment_id, file_sha256, "
            " filename, container, parent_attachment_id, parent_filename, "
            " member_path, local_path, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_key, str(chat_id), gmail_message_id, attachment_id, file_sha256,
             filename, container, parent_attachment_id, parent_filename,
             member_path, local_path, DETECTED, _now(), _now()),
        )
        conn.commit()
        return cur.rowcount == 1


def get_document(db_path: str, doc_key: str) -> dict[str, Any] | None:
    import sqlite3

    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM documents WHERE doc_key = ?", (doc_key,)
        ).fetchone()
        return dict(row) if row else None


def update_document(db_path: str, doc_key: str, **fields: Any) -> None:
    """Enregistre un point de reprise. Chaque etape reussie est persistee."""
    allowed = (
        "state", "doc_type", "numero", "stable_id", "tab", "row_index",
        "lines_written", "drive_link", "calendar_event", "log_row", "payload",
        "error", "attachment_id", "parent_attachment_id", "parent_filename",
        "member_path", "local_path", "review_archive",
    )
    columns = [k for k in fields if k in allowed]
    if not columns:
        return
    assignments = ", ".join(f"{c} = ?" for c in columns)
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE documents SET {assignments}, updated_at = ? WHERE doc_key = ?",
            (*(fields[c] for c in columns), _now(), doc_key),
        )
        conn.commit()


def set_state(db_path: str, doc_key: str, state: str, *, error: str = "") -> None:
    update_document(db_path, doc_key, state=state, error=error)


def release_document(db_path: str, doc_key: str) -> None:
    """Libere une reservation dont AUCUNE ecriture n'a abouti.

    Ne doit jamais etre appelee une fois la ligne comptable ecrite : ce
    serait rouvrir la porte a une seconde ligne.
    """
    with connect(db_path) as conn:
        conn.execute(
            "DELETE FROM documents WHERE doc_key = ? AND state NOT IN "
            "('sheet_written','details_written','drive_archived','calendar_created',"
            "'logged','completed','partial')",
            (doc_key,),
        )
        conn.commit()


def find_by_sha256(db_path: str, chat_id: int, file_sha256: str) -> dict[str, Any] | None:
    """Le meme fichier a-t-il deja ete traite, meme via un autre email ?"""
    import sqlite3

    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? AND file_sha256 = ? "
            "AND state IN ('completed','partial','sheet_written','details_written',"
            "'drive_archived','calendar_created','logged') ORDER BY created_at LIMIT 1",
            (str(chat_id), file_sha256),
        ).fetchone()
        return dict(row) if row else None


def find_by_business_key(
    db_path: str, chat_id: int, doc_type: str, numero: str
) -> dict[str, Any] | None:
    """Doublon metier : meme type et meme numero pour ce client."""
    import sqlite3

    if not numero:
        return None
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? AND doc_type = ? "
            "AND UPPER(numero) = UPPER(?) AND stable_id IS NOT NULL AND stable_id != '' "
            "ORDER BY created_at LIMIT 1",
            (str(chat_id), doc_type, numero),
        ).fetchone()
        return dict(row) if row else None


def find_by_key_prefix(db_path: str, chat_id: int, prefix: str) -> dict[str, Any] | None:
    """Retrouve un document par le debut de sa cle.

    Telegram limite `callback_data` a 64 octets : le bouton ne peut pas
    porter la cle complete. Le prefixe est cloisonne par `chat_id`, donc
    un client ne peut jamais atteindre le document d'un autre.
    """
    import sqlite3

    if not prefix:
        return None
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? AND doc_key LIKE ? || '%' "
            "ORDER BY created_at LIMIT 1",
            (str(chat_id), prefix),
        ).fetchone()
        return dict(row) if row else None


def find_by_message_and_sha(
    db_path: str, chat_id: int, gmail_message_id: str, file_sha256: str
) -> dict[str, Any] | None:
    """Le MEME fichier, dans le MEME email : c'est le meme document.

    Point d'ancrage stable du pipeline. La cle d'idempotence a d'abord ete
    calculee a partir de l'`attachmentId` de Gmail, qui change d'une lecture
    a l'autre : chaque cycle recreait alors un document neuf pour une piece
    deja traitee. Ce couple (email, empreinte), lui, ne bouge jamais.
    """
    import sqlite3

    if not gmail_message_id or not file_sha256:
        return None
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? AND gmail_message_id = ? "
            "AND file_sha256 = ? ORDER BY created_at LIMIT 1",
            (str(chat_id), gmail_message_id, file_sha256),
        ).fetchone()
        return dict(row) if row else None


def list_pending_review(db_path: str, chat_id: int) -> list[dict[str, Any]]:
    """Documents qui attendent reellement une decision humaine."""
    import sqlite3

    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? AND state = ? ORDER BY created_at",
            (str(chat_id), NEEDS_REVIEW),
        ).fetchall()
        return [dict(r) for r in rows]


def list_unfinished(db_path: str, chat_id: int) -> list[dict[str, Any]]:
    """Documents dont l'ecriture comptable a abouti mais pas la suite."""
    import sqlite3

    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? AND state IN "
            "('partial','sheet_written','details_written','drive_archived',"
            "'calendar_created') ORDER BY created_at",
            (str(chat_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def list_by_message(db_path: str, chat_id: int, gmail_message_id: str) -> list[dict[str, Any]]:
    import sqlite3

    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? AND gmail_message_id = ? "
            "ORDER BY created_at",
            (str(chat_id), gmail_message_id),
        ).fetchall()
        return [dict(r) for r in rows]


# --- lignes bancaires -----------------------------------------------------

def claim_bank_line(db_path: str, chat_id: int, fingerprint: str, row_index: int = 0) -> bool:
    """Reserve une operation bancaire. False si elle existe deja.

    Deux releves qui se chevauchent ne creent pas deux fois la meme ligne.
    """
    if not fingerprint:
        return True
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bank_line_fingerprints "
            "(fingerprint, chat_id, row_index, created_at) VALUES (?,?,?,?)",
            (fingerprint, str(chat_id), row_index, _now()),
        )
        conn.commit()
        return cur.rowcount == 1


# --- evenements Calendar --------------------------------------------------

def claim_calendar_event(db_path: str, chat_id: int, event_key: str) -> bool:
    if not event_key:
        return True
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO calendar_events (event_key, chat_id, created_at) "
            "VALUES (?,?,?)",
            (event_key, str(chat_id), _now()),
        )
        conn.commit()
        return cur.rowcount == 1


def record_calendar_event(db_path: str, event_key: str, event_id: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE calendar_events SET event_id = ? WHERE event_key = ?",
            (event_id, event_key),
        )
        conn.commit()


# --- curseur Gmail --------------------------------------------------------
# Le curseur AVANCE : il retient la date du dernier email traite, et la
# requete repart un peu avant (fenetre de recouvrement) pour ne jamais
# perdre un email arrive pendant un cycle. Les memes emails ne sont pas
# retelecharges : l'idempotence par document s'en charge.

OVERLAP_SECONDS = 300


def get_or_init_cursor(db_path: str, chat_id: int, now_epoch: int) -> dict[str, Any]:
    import sqlite3

    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO gmail_sync_state "
            "(chat_id, history_id, last_internal_date, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (str(chat_id), None, int(now_epoch), _now(), _now()),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM gmail_sync_state WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        return dict(row)


def advance_cursor(
    db_path: str, chat_id: int, last_internal_date: int, history_id: str | None = None
) -> None:
    """Avance le curseur, jamais en arriere."""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE gmail_sync_state SET last_internal_date = MAX(last_internal_date, ?), "
            "history_id = COALESCE(?, history_id), updated_at = ? WHERE chat_id = ?",
            (int(last_internal_date), history_id, _now(), str(chat_id)),
        )
        conn.commit()


def rewind_cursor(db_path: str, chat_id: int, seconds: int) -> int:
    """Recul volontaire du curseur (commande /reprocess).

    Les documents deja traites restent proteges par leur cle d'idempotence :
    reculer le curseur fait relire des emails, pas reecrire des lignes.
    """
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE gmail_sync_state SET last_internal_date = MAX(0, last_internal_date - ?), "
            "updated_at = ? WHERE chat_id = ?",
            (int(seconds), _now(), str(chat_id)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT last_internal_date FROM gmail_sync_state WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
        return int(row[0]) if row else 0


def query_floor(cursor: dict[str, Any]) -> int:
    """Borne `after:` a envoyer a Gmail, recouvrement compris."""
    return max(0, int(cursor["last_internal_date"]) - OVERLAP_SECONDS)
