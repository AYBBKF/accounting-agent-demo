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
# Fiche conservee pour l'audit, mais rattachee a une fiche canonique :
# le MEME fichier etait deja connu, dans un autre email ou hors ZIP.
# On ne supprime jamais la fiche secondaire, on la marque.
SUPERSEDED = "superseded"

ALL_STATES = (
    DETECTED, DOWNLOADED, EXTRACTED, VALIDATED, SHEET_WRITTEN, DETAILS_WRITTEN,
    DRIVE_ARCHIVED, CALENDAR_CREATED, LOGGED, COMPLETED, NEEDS_REVIEW, SKIPPED,
    DUPLICATE, PARTIAL, FAILED, SUPERSEDED,
)

# Etats a partir desquels la ligne comptable EXISTE deja : on ne la reecrit
# jamais, on ne fait que terminer les etapes suivantes.
STATES_AFTER_SHEET = frozenset({
    SHEET_WRITTEN, DETAILS_WRITTEN, DRIVE_ARCHIVED, CALENDAR_CREATED, LOGGED,
    PARTIAL,
})
TERMINAL_STATES = frozenset({COMPLETED, SKIPPED, DUPLICATE, SUPERSEDED})

# Etats OUVERTS : le document existe, il n'a produit AUCUNE ecriture
# comptable, et il attend encore quelque chose - une verification
# humaine, un classement, ou une reprise apres echec. C'est exactement
# la population que l'ancienne deduplication ne regardait pas : elle ne
# reconnaissait un fichier deja vu que s'il avait ete COMPTABILISE.
# Le meme PDF renvoye hors ZIP repartait donc de zero et creait une
# seconde ligne rouge dans 21_A_VERIFIER.
#
# SKIPPED porte le document de type inconnu, FAILED le document dont la
# lecture a echoue : tous deux se dupliquaient de la meme facon.
OPEN_STATES = frozenset({
    DETECTED, DOWNLOADED, EXTRACTED, VALIDATED, NEEDS_REVIEW, SKIPPED, FAILED,
})

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
    doc_key TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_events (
    event_key TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    event_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_notifications (
    chat_id TEXT NOT NULL,
    gmail_message_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, gmail_message_id)
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
    # Identite COMPTABLE du document, figee au moment ou il entre en
    # quarantaine. Le `doc_key` contient l'identifiant du message
    # Gmail : il change d'un email a l'autre pour un meme document.
    # Cette colonne, elle, permet de retrouver la ligne 21_A_VERIFIER
    # deja ouverte pour ce document et de ne pas en creer une seconde.
    ("business_key", "TEXT"),
    # Idempotence des NOTIFICATIONS. Sans ces colonnes, chaque cycle Gmail
    # renvoyait les memes messages : l'etat du document etait connu, mais
    # jamais l'etat DEJA NOTIFIE.
    ("last_notified_state", "TEXT"),
    ("notified_at", "TEXT"),
    ("validation_notification_sent_at", "TEXT"),
    ("telegram_message_id", "INTEGER DEFAULT 0"),
    # Ligne occupee par ce document dans 21_A_VERIFIER. Sans elle, chaque
    # cycle Gmail devrait relire tout l'onglet pour savoir si le document
    # y figure deja - et un onglet relu 60 fois par heure finit par heurter
    # les quotas Sheets.
    ("review_row", "INTEGER DEFAULT 0"),
    # Fiche remplacee par une autre, apres deduplication metier. On ne
    # SUPPRIME jamais une fiche d'audit : elle reste, marquee, pour que
    # l'historique reste verifiable.
    ("superseded_by", "TEXT"),
)



# --- portee par entreprise (multi-tenant) ---------------------------------
#
# Chaque recherche qui DECIDE d'une ecriture comptable - doublon physique,
# doublon metier, jumeau ouvert, reprise - doit rester enfermee dans
# l'entreprise routee. Deux societes peuvent legitimement recevoir la meme
# facture, portant le meme numero et le meme hash : sans cette portee, la
# seconde serait prise pour un doublon de la premiere et ne serait jamais
# comptabilisee.
#
# `company_id=""` conserve exactement l'ancien comportement, ce qui permet
# a la base mono-entreprise et a ses tests de continuer a fonctionner sans
# modification pendant la migration.

def _scope(company_id: str) -> tuple[str, tuple[str, ...]]:
    """Fragment SQL et parametres qui enferment une requete dans un tenant."""
    if not company_id:
        return "", ()
    return " AND company_id = ?", (str(company_id),)


def _has_company_column(conn: Any, table: str) -> bool:
    return any(
        row[1] == "company_id" for row in conn.execute(f"PRAGMA table_info({table})")
    )


def ensure_schema(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        for column, kind in _ADDED_COLUMNS:
            if column not in existing:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {column} {kind}")
        colonnes_bancaires = {
            row[1] for row in conn.execute("PRAGMA table_info(bank_line_fingerprints)")
        }
        if "doc_key" not in colonnes_bancaires:
            conn.execute("ALTER TABLE bank_line_fingerprints ADD COLUMN doc_key TEXT")
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
    company_id: str = "",
) -> bool:
    """Reserve un document. True s'il est nouveau, False s'il est deja connu.

    L'insertion est atomique : deux cycles concurrents ne peuvent pas
    reserver le meme document.
    """
    with connect(db_path) as conn:
        colonnes = ("doc_key, chat_id, gmail_message_id, attachment_id, file_sha256,"
                    " filename, container, parent_attachment_id, parent_filename,"
                    " member_path, local_path, state, created_at, updated_at")
        valeurs = [doc_key, str(chat_id), gmail_message_id, attachment_id, file_sha256,
                   filename, container, parent_attachment_id, parent_filename,
                   member_path, local_path, DETECTED, _now(), _now()]
        if _has_company_column(conn, "documents"):
            colonnes += ", company_id"
            valeurs.append(str(company_id))
        trous = ",".join("?" * len(valeurs))
        cur = conn.execute(
            f"INSERT OR IGNORE INTO documents ({colonnes}) VALUES ({trous})",
            tuple(valeurs),
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
        "member_path", "local_path", "review_archive", "review_row",
        "superseded_by", "business_key",
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

    Une fiche qui porte deja une archive Drive ou une ligne de journal n'est
    PAS "sans ecriture" : la supprimer faisait oublier le document, et le
    cycle suivant le recreait de zero - nouvelle cle, nouvelle copie dans
    Drive, nouvelle ligne dans 14_IMPORTS_LOG, nouvelle fiche tiers. C'est
    exactement ce qui s'est produit en production quand une validation
    humaine a echoue sur un document en attente. On protege donc aussi
    'needs_review' et toute fiche deja archivee ou journalisee.
    """
    with connect(db_path) as conn:
        conn.execute(
            "DELETE FROM documents WHERE doc_key = ? AND state NOT IN "
            "('sheet_written','details_written','drive_archived','calendar_created',"
            "'logged','completed','partial','needs_review','skipped','duplicate') "
            "AND COALESCE(drive_link, '') = '' AND COALESCE(log_row, 0) = 0",
            (doc_key,),
        )
        conn.commit()


def find_by_sha256(
    db_path: str, chat_id: int, file_sha256: str, *, company_id: str = ""
) -> dict[str, Any] | None:
    """Le meme fichier a-t-il deja ete traite, DANS CETTE ENTREPRISE ?"""
    import sqlite3

    portee, params = _scope(company_id)
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? AND file_sha256 = ? "
            "AND state IN ('completed','partial','sheet_written','details_written',"
            f"'drive_archived','calendar_created','logged'){portee}"
            " ORDER BY created_at LIMIT 1",
            (str(chat_id), file_sha256, *params),
        ).fetchone()
        return dict(row) if row else None


def find_open_twin(
    db_path: str, chat_id: int, file_sha256: str, *, exclude_key: str = "",
    company_id: str = "",
) -> dict[str, Any] | None:
    """Le MEME fichier est-il deja connu, sans avoir rien produit ?

    Complement indispensable de `find_by_sha256`, qui ne regarde que les
    documents deja COMPTABILISES. Un document en quarantaine, de type
    inconnu, ou dont la lecture a echoue, restait invisible : le meme PDF
    renvoye dans un second email repartait de zero et ajoutait une seconde
    ligne rouge dans `21_A_VERIFIER` pour un seul document physique.

    On rend la fiche CANONIQUE, c'est-a-dire la plus ancienne, et jamais
    une fiche deja rattachee a une autre : sans cela une chaine de
    rattachements pourrait se former et le lien remonterait a un document
    lui-meme secondaire.
    """
    import sqlite3

    if not file_sha256:
        return None
    portee, params = _scope(company_id)
    placeholders = ",".join("?" * len(OPEN_STATES))
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? AND file_sha256 = ? "
            f"AND state IN ({placeholders}) "
            "AND (superseded_by IS NULL OR superseded_by = '') "
            f"AND doc_key != ?{portee} ORDER BY created_at LIMIT 1",
            (str(chat_id), file_sha256, *sorted(OPEN_STATES), exclude_key or "",
             *params),
        ).fetchone()
        return dict(row) if row else None


def list_quarantined(
    db_path: str, chat_id: int, *, company_id: str = ""
) -> list[dict[str, Any]]:
    """Fiches qui occupent DEJA une ligne de `21_A_VERIFIER`.

    Sert a garantir une ligne par document PHYSIQUE : avant d'en ecrire une
    nouvelle, on cherche parmi celles-ci un document de meme identite
    metier. Les fiches rattachees sont exclues - leur ligne est celle de
    leur canonique.
    """
    import sqlite3

    portee, params = _scope(company_id)
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? AND review_row > 0 "
            "AND (superseded_by IS NULL OR superseded_by = '')"
            f"{portee} ORDER BY created_at",
            (str(chat_id), *params),
        ).fetchall()
        return [dict(r) for r in rows]


def find_by_business_key(
    db_path: str, chat_id: int, doc_type: str, numero: str, *, company_id: str = ""
) -> dict[str, Any] | None:
    """Doublon metier : meme type et meme numero DANS CETTE ENTREPRISE.

    Deux societes peuvent recevoir des factures portant le meme numero de
    la part de fournisseurs differents : le numero n'est unique que dans
    la comptabilite qui le recoit.
    """
    import sqlite3

    if not numero:
        return None
    portee, params = _scope(company_id)
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? AND doc_type = ? "
            "AND UPPER(numero) = UPPER(?) AND stable_id IS NOT NULL "
            f"AND stable_id != ''{portee} ORDER BY created_at LIMIT 1",
            (str(chat_id), doc_type, numero, *params),
        ).fetchone()
        return dict(row) if row else None


def find_by_stable_id(db_path: str, stable_id: str) -> dict[str, Any] | None:
    """Retrouve la fiche d'une ecriture par son identifiant comptable."""
    import sqlite3

    if not stable_id:
        return None
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM documents WHERE stable_id = ? ORDER BY created_at LIMIT 1",
            (stable_id,),
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
    db_path: str, chat_id: int, gmail_message_id: str, file_sha256: str,
    *, company_id: str = "",
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
    portee, params = _scope(company_id)
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? AND gmail_message_id = ? "
            f"AND file_sha256 = ?{portee} ORDER BY created_at LIMIT 1",
            (str(chat_id), gmail_message_id, file_sha256, *params),
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


def list_documents(db_path: str, chat_id: int) -> list[dict[str, Any]]:
    """Toutes les fiches d'un client, du plus ancien au plus recent."""
    import sqlite3

    ensure_schema(db_path)
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? ORDER BY created_at",
            (str(chat_id),),
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

def claim_bank_line(
    db_path: str, chat_id: int, fingerprint: str, row_index: int = 0,
    doc_key: str = "", *, company_id: str = "",
) -> bool:
    """Reserve une operation bancaire. False si elle existe deja.

    On memorise DESORMAIS quel document l'a reservee. Sans cette
    information, deux situations opposees etaient indiscernables :
      - le meme releve reecrit, ou la ligne existante est la bonne ;
      - un mouvement REELLEMENT repete, qui doit etre signale.
    L'ancien code les traitait toutes deux en supprimant la seconde ligne,
    en silence.
    """
    if not fingerprint:
        return True
    with connect(db_path) as conn:
        colonnes = "fingerprint, chat_id, row_index, doc_key, created_at"
        valeurs = [fingerprint, str(chat_id), row_index, doc_key or "", _now()]
        if _has_company_column(conn, "bank_line_fingerprints"):
            colonnes += ", company_id"
            valeurs.append(str(company_id))
        trous = ",".join("?" * len(valeurs))
        cur = conn.execute(
            "INSERT OR IGNORE INTO bank_line_fingerprints "
            f"({colonnes}) VALUES ({trous})",
            tuple(valeurs),
        )
        conn.commit()
        return cur.rowcount == 1


def bank_line_owner(db_path: str, fingerprint: str) -> str:
    """Quel document a enregistre cette operation en premier ?"""
    if not fingerprint:
        return ""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT doc_key FROM bank_line_fingerprints WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        return str(row[0] or "") if row else ""


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


def get_or_init_cursor(
    db_path: str, chat_id: int, now_epoch: int, *, company_id: str = ""
) -> dict[str, Any]:
    """Curseur Gmail. Chaque entreprise avance le sien.

    Un curseur partage ferait sauter des emails : l'entreprise servie en
    premier avancerait la borne pour toutes les autres.
    """
    import sqlite3

    portee, params = _scope(company_id)
    with connect(db_path) as conn:
        colonnes = "chat_id, history_id, last_internal_date, created_at, updated_at"
        valeurs = [str(chat_id), None, int(now_epoch), _now(), _now()]
        if _has_company_column(conn, "gmail_sync_state"):
            colonnes += ", company_id"
            valeurs.append(str(company_id))
        trous = ",".join("?" * len(valeurs))
        conn.execute(
            f"INSERT OR IGNORE INTO gmail_sync_state ({colonnes}) VALUES ({trous})",
            tuple(valeurs),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT * FROM gmail_sync_state WHERE chat_id = ?{portee}",
            (str(chat_id), *params),
        ).fetchone()
        return dict(row)


def advance_cursor(
    db_path: str, chat_id: int, last_internal_date: int, history_id: str | None = None,
    *, company_id: str = "",
) -> None:
    """Avance le curseur de CETTE entreprise, jamais en arriere."""
    portee, params = _scope(company_id)
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE gmail_sync_state SET last_internal_date = MAX(last_internal_date, ?), "
            "history_id = COALESCE(?, history_id), updated_at = ? "
            f"WHERE chat_id = ?{portee}",
            (int(last_internal_date), history_id, _now(), str(chat_id), *params),
        )
        conn.commit()


def rewind_cursor(
    db_path: str, chat_id: int, seconds: int, *, company_id: str = ""
) -> int:
    """Recul volontaire du curseur (commande /reprocess).

    Les documents deja traites restent proteges par leur cle d'idempotence :
    reculer le curseur fait relire des emails, pas reecrire des lignes.

    La portee est celle d'UNE entreprise. Sans elle, un `/reprocess`
    demande pour une comptabilite reculait aussi le curseur de toutes
    celles qui partagent le meme canal Telegram : elles relisaient des
    semaines d'emails que personne n'avait demande de relire.
    """
    portee, params = _scope(company_id)
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE gmail_sync_state SET last_internal_date = MAX(0, last_internal_date - ?), "
            f"updated_at = ? WHERE chat_id = ?{portee}",
            (int(seconds), _now(), str(chat_id), *params),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT last_internal_date FROM gmail_sync_state WHERE chat_id = ?{portee}",
            (str(chat_id), *params),
        ).fetchone()
        return int(row[0]) if row else 0


def query_floor(cursor: dict[str, Any]) -> int:
    """Borne `after:` a envoyer a Gmail, recouvrement compris."""
    return max(0, int(cursor["last_internal_date"]) - OVERLAP_SECONDS)


# --- idempotence des notifications ---------------------------------------
#
# Un document connait son etat metier ; il ne connaissait pas l'etat DEJA
# ANNONCE au client. C'est exactement ce qui produisait un message identique
# a chaque cycle Gmail. On memorise donc, durablement, le dernier etat
# notifie - et, pour une demande de validation, l'instant de l'envoi et
# l'identifiant du message Telegram, afin qu'un redemarrage ne provoque
# aucun renvoi.

def notification_state(db_path: str, doc_key: str) -> str:
    """Dernier etat REELLEMENT annonce au client, '' si jamais notifie."""
    ensure_schema(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_notified_state FROM documents WHERE doc_key = ?", (doc_key,)
        ).fetchone()
    if not row:
        return ""
    return str(row[0] or "")


def mark_notified(
    db_path: str, doc_key: str, state: str, *, telegram_message_id: int = 0
) -> None:
    """Enregistre qu'un etat a ete annonce. Appele APRES l'envoi reussi."""
    ensure_schema(db_path)
    now = _now()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT validation_notification_sent_at FROM documents WHERE doc_key = ?",
            (doc_key,),
        ).fetchone()
        if row is None:
            return
        fields = {"last_notified_state": state, "notified_at": now}
        if state == "waiting_validation" and not (row[0] or ""):
            fields["validation_notification_sent_at"] = now
        if telegram_message_id:
            fields["telegram_message_id"] = int(telegram_message_id)
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(
            f"UPDATE documents SET {assignments}, updated_at = ? WHERE doc_key = ?",
            (*fields.values(), now, doc_key),
        )
        conn.commit()


def email_notification_signature(
    db_path: str, chat_id: int, message_id: str, *, company_id: str = ""
) -> str:
    """Signature du dernier resume envoye a CETTE entreprise ('' si aucun).

    Un meme email peut concerner deux entreprises : chacune doit recevoir
    son resume, et n'etre reduite au silence que par le sien.
    """
    ensure_schema(db_path)
    portee, params = _scope(company_id)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT signature FROM email_notifications WHERE chat_id = ? "
            f"AND gmail_message_id = ?{portee}",
            (str(chat_id), str(message_id), *params),
        ).fetchone()
    return str(row[0]) if row else ""


def remember_email_notification(
    db_path: str, chat_id: int, message_id: str, signature: str,
    *, company_id: str = "",
) -> None:
    ensure_schema(db_path)
    now = _now()
    with connect(db_path) as conn:
        # La cle de conflit suit la forme reelle de la table : elle gagne
        # `company_id` a la migration, et l'ecriture doit suivre sous peine
        # d'ecraser la notification d'une autre entreprise.
        if _has_company_column(conn, "email_notifications"):
            conn.execute(
                "INSERT INTO email_notifications (company_id, chat_id, "
                "gmail_message_id, signature, sent_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(company_id, chat_id, gmail_message_id) "
                "DO UPDATE SET signature = excluded.signature, "
                "sent_at = excluded.sent_at",
                (str(company_id), str(chat_id), str(message_id), signature, now),
            )
        else:
            conn.execute(
                "INSERT INTO email_notifications (chat_id, gmail_message_id, "
                "signature, sent_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id, gmail_message_id) "
                "DO UPDATE SET signature = excluded.signature, "
                "sent_at = excluded.sent_at",
                (str(chat_id), str(message_id), signature, now),
            )
        conn.commit()
