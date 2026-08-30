"""Registre des archives Drive, par entreprise.

L'archive Drive sans registre est une promesse invérifiable : on ne peut
ni prouver qu'une piece a ete archivee, ni empecher de l'archiver deux
fois, ni retrouver l'archive depuis l'ecriture comptable. Ce registre
porte, pour chaque fichier archive : son empreinte, son origine Gmail,
sa reference comptable et son identifiant Drive.

La cle primaire (entreprise, sha256) rend structurel ce que le cahier
des charges exige : un meme contenu ne s'archive qu'UNE fois dans une
entreprise, et peut s'archiver une fois dans CHAQUE entreprise.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS drive_archives (
    company_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    original_name TEXT NOT NULL DEFAULT '',
    mimetype TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    gmail_message_id TEXT NOT NULL DEFAULT '',
    reference TEXT NOT NULL DEFAULT '',
    statut TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    drive_file_id TEXT NOT NULL DEFAULT '',
    drive_link TEXT NOT NULL DEFAULT '',
    sheet_ref TEXT NOT NULL DEFAULT '',
    archived_at TEXT NOT NULL,
    PRIMARY KEY (company_id, sha256)
);
"""


def ensure_schema(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def known(db_path: str, company_id: str, sha256: str) -> dict | None:
    """L'archive existante pour ce contenu dans CETTE entreprise, ou None."""
    ensure_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM drive_archives WHERE company_id=? AND sha256=?",
            (company_id, sha256),
        ).fetchone()
    return dict(row) if row else None


def remember(
    db_path: str, *, company_id: str, sha256: str, original_name: str = "",
    mimetype: str = "", size_bytes: int = 0, gmail_message_id: str = "",
    reference: str = "", statut: str = "", category: str = "",
    drive_file_id: str = "", drive_link: str = "", sheet_ref: str = "",
) -> bool:
    """Enregistre UNE archive. Rend False si le contenu etait deja archive.

    Un rejeu (reprise apres panne, second email portant les memes octets)
    retombe sur l'archive existante au lieu d'en creer une seconde.
    """
    if not company_id.strip() or not sha256.strip():
        raise ValueError("une archive exige une entreprise et une empreinte")
    ensure_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO drive_archives (company_id, sha256, original_name,"
                " mimetype, size_bytes, gmail_message_id, reference, statut,"
                " category, drive_file_id, drive_link, sheet_ref, archived_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    company_id, sha256, original_name, mimetype, int(size_bytes),
                    gmail_message_id, reference, statut, category,
                    drive_file_id, drive_link, sheet_ref,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def update_status(
    db_path: str, company_id: str, sha256: str, *, statut: str = "",
    reference: str = "", sheet_ref: str = "",
) -> None:
    """Complete une archive existante (statut, reference comptable)."""
    ensure_schema(db_path)
    champs, params = [], []
    for nom, valeur in (("statut", statut), ("reference", reference),
                        ("sheet_ref", sheet_ref)):
        if valeur:
            champs.append(f"{nom}=?")
            params.append(valeur)
    if not champs:
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"UPDATE drive_archives SET {', '.join(champs)}"
            " WHERE company_id=? AND sha256=?",
            (*params, company_id, sha256),
        )
        conn.commit()


def archives_for(db_path: str, company_id: str) -> list[dict]:
    ensure_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM drive_archives WHERE company_id=? ORDER BY archived_at",
            (company_id,),
        )]
