"""Comptabilite des appels LLM, entreprise par entreprise.

Un agent multi-entreprises facture des appels de modele pour le compte de
plusieurs clients. Sans ventilation, le cout total est un chiffre unique
que personne ne peut imputer ni contester. Ce journal repond a trois
questions concretes :

  * quelle entreprise a consomme quoi ;
  * pourquoi le pipeline a escalade vers un modele plus cher ;
  * quelle piece a repaye une escalade qu'elle n'aurait pas du repayer.

Deux interdits absolus, verifies par les tests :

  * AUCUN secret, ni cle, ni identifiant d'API ;
  * AUCUN contenu de document - pas de base64, pas de texte OCR. Le
    journal des couts n'est pas un endroit ou stocker des factures.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

# Motifs d'escalade normalises. Une chaine libre rendrait toute
# statistique impossible : on ne saurait pas regrouper « OCR illisible »
# et « ocr illisible ».
REASON_DIRECT = "direct"                    # lecture deterministe suffisante
REASON_MISSING_FIELDS = "champs_manquants"
REASON_INCOHERENT_TOTALS = "totaux_incoherents"
REASON_LOW_CONFIDENCE = "confiance_insuffisante"
REASON_UNREADABLE_IMAGE = "image_illisible"

OUTCOME_ACCEPTED = "retenu"
OUTCOME_REJECTED = "rejete"
OUTCOME_EMPTY = "vide"
OUTCOME_UNAVAILABLE = "indisponible"

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id TEXT NOT NULL,
    doc_key TEXT NOT NULL DEFAULT '',
    gmail_message_id TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL,
    model TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_company
    ON llm_usage(company_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_usage_doc
    ON llm_usage(company_id, doc_key);
"""

# Colonnes autorisees. Toute tentative d'ecrire ailleurs est un bug qu'on
# veut voir exploser en test plutot que fuiter en production.
_ALLOWED_FIELDS = frozenset({
    "company_id", "doc_key", "gmail_message_id", "level", "model", "reason",
    "outcome", "input_tokens", "output_tokens", "estimated_cost_usd",
})


class UsageError(RuntimeError):
    """Ecriture refusee par le journal des couts."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@dataclass(frozen=True)
class UsageTotals:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


def record_call(
    db_path: str,
    *,
    company_id: str,
    level: str,
    model: str,
    doc_key: str = "",
    gmail_message_id: str = "",
    reason: str = "",
    outcome: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
) -> None:
    """Enregistre UN appel de modele, impute a une entreprise.

    `company_id` est obligatoire : un appel qu'on ne sait pas imputer est
    un appel qu'on ne sait pas facturer, et le silence coute plus cher
    qu'une erreur bruyante.
    """
    if not str(company_id).strip():
        raise UsageError("un appel LLM doit toujours etre impute a une entreprise")
    ensure_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO llm_usage (company_id, doc_key, gmail_message_id, level,"
            " model, reason, outcome, input_tokens, output_tokens,"
            " estimated_cost_usd, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(company_id), doc_key, gmail_message_id, level, model,
                reason, outcome, int(input_tokens), int(output_tokens),
                float(estimated_cost_usd), _now(),
            ),
        )
        conn.commit()


def totals_for(db_path: str, company_id: str) -> UsageTotals:
    ensure_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0),"
            " COALESCE(SUM(output_tokens),0), COALESCE(SUM(estimated_cost_usd),0.0)"
            " FROM llm_usage WHERE company_id = ?",
            (str(company_id),),
        ).fetchone()
    return UsageTotals(int(row[0]), int(row[1]), int(row[2]), float(row[3]))


def totals_by_company(db_path: str) -> dict[str, UsageTotals]:
    """Ventilation complete. C'est la vue qui part dans un rapport client."""
    ensure_schema(db_path)
    sortie: dict[str, UsageTotals] = {}
    with sqlite3.connect(db_path) as conn:
        for ligne in conn.execute(
            "SELECT company_id, COUNT(*), COALESCE(SUM(input_tokens),0),"
            " COALESCE(SUM(output_tokens),0), COALESCE(SUM(estimated_cost_usd),0.0)"
            " FROM llm_usage GROUP BY company_id ORDER BY company_id"
        ):
            sortie[ligne[0]] = UsageTotals(
                int(ligne[1]), int(ligne[2]), int(ligne[3]), float(ligne[4])
            )
    return sortie


def calls_for_document(db_path: str, company_id: str, doc_key: str) -> int:
    """Combien d'appels cette piece a-t-elle deja coute ?

    Sert de preuve pour la regle « une piece deja traitee ou deja en
    quarantaine ne repaye jamais une escalade » : ce compteur doit cesser
    d'augmenter des le second cycle.
    """
    ensure_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM llm_usage WHERE company_id = ? AND doc_key = ?",
            (str(company_id), doc_key),
        ).fetchone()
    return int(row[0])


def levels_for_document(db_path: str, company_id: str, doc_key: str) -> tuple[str, ...]:
    """Niveaux appeles pour cette piece, dans l'ordre.

    Rend l'escalade verifiable : ('terra',) pour une relecture texte
    suffisante, ('terra', 'sol') quand il a fallu monter jusqu'a l'image.
    """
    ensure_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        lignes = conn.execute(
            "SELECT level FROM llm_usage WHERE company_id = ? AND doc_key = ?"
            " ORDER BY id",
            (str(company_id), doc_key),
        ).fetchall()
    return tuple(str(l[0]) for l in lignes)
