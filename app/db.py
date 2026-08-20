"""Acces SQLite minimal pour la demo (pas de Postgres/Redis).

Toutes les colonnes monetaires sont stockees en TEXT et manipulees
exclusivement via Decimal cote application (jamais de float).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    date_operation TEXT NOT NULL,
    libelle TEXT NOT NULL,
    montant TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS demo_reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL,
    bank_line_id INTEGER,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (invoice_id) REFERENCES demo_invoices (id),
    FOREIGN KEY (bank_line_id) REFERENCES demo_bank_lines (id)
);
"""


def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def connect(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
