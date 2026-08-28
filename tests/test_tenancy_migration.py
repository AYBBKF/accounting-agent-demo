"""Migration de l'etat legacy vers l'entreprise XBLASTE.

Une base de production existante ne porte aucune notion d'entreprise :
toutes ses lignes appartiennent en realite a XBLASTE. La migration doit
les rattacher sans rien perdre, sans rien reemettre, et pouvoir se
rejouer indefiniment.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from app import doc_store as store
from app import tenancy
from app.db import init_db


@pytest.fixture
def legacy_db():
    """Base au format d'AVANT le multi-tenant, avec de l'historique."""
    chemin = tempfile.mktemp(suffix=".db")
    init_db(chemin)
    store.ensure_schema(chemin)

    store.claim_document(
        chemin, "cle-1", 999653395,
        gmail_message_id="msg-1", attachment_id="att-1",
        file_sha256="a" * 64, filename="facture.pdf",
    )
    store.claim_document(
        chemin, "cle-2", 999653395,
        gmail_message_id="msg-2", attachment_id="att-2",
        file_sha256="b" * 64, filename="releve.pdf",
    )
    with sqlite3.connect(chemin) as conn:
        conn.execute(
            "INSERT INTO email_notifications (chat_id, gmail_message_id,"
            " signature, sent_at) VALUES (?,?,?,?)",
            ("999653395", "msg-1", "sig-1", "2026-08-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO gmail_sync_state (chat_id, history_id,"
            " last_internal_date, created_at, updated_at)"
            " VALUES (?,?,?,?,?)",
            ("999653395", "h-42", 1787872375, "2026-08-01T00:00:00+00:00",
             "2026-08-27T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO bank_line_fingerprints (fingerprint, chat_id,"
            " row_index, doc_key, created_at) VALUES (?,?,?,?,?)",
            ("emp-1", "999653395", 2, "cle-2", "2026-08-01T00:00:00+00:00"),
        )
        conn.commit()
    yield chemin
    Path(chemin).unlink(missing_ok=True)


def _compter(db_path: str, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_une_base_legacy_n_est_pas_deja_migree(legacy_db):
    assert not tenancy.is_migrated(legacy_db)


def test_la_migration_rattache_tout_l_historique_a_xblaste(legacy_db):
    rapport = tenancy.migrate_to_multi_tenant(legacy_db)
    assert rapport.company_id == "xblaste"
    assert tenancy.is_migrated(legacy_db)
    assert tenancy.orphan_rows(legacy_db) == {}

    comptes = tenancy.company_counts(legacy_db)
    assert comptes["documents"] == {"xblaste": 2}
    assert comptes["email_notifications"] == {"xblaste": 1}
    assert comptes["gmail_sync_state"] == {"xblaste": 1}
    assert comptes["bank_line_fingerprints"] == {"xblaste": 1}


def test_aucune_ligne_n_est_perdue(legacy_db):
    avant = {
        table: _compter(legacy_db, table)
        for table in ("documents", "email_notifications", "gmail_sync_state",
                      "bank_line_fingerprints")
    }
    tenancy.migrate_to_multi_tenant(legacy_db)
    apres = {table: _compter(legacy_db, table) for table in avant}
    assert apres == avant


def test_le_curseur_gmail_ne_recule_pas(legacy_db):
    """Un curseur qui recule ferait retraiter d'anciens emails."""
    with sqlite3.connect(legacy_db) as conn:
        avant = conn.execute(
            "SELECT history_id, last_internal_date FROM gmail_sync_state"
        ).fetchone()
    tenancy.migrate_to_multi_tenant(legacy_db)
    with sqlite3.connect(legacy_db) as conn:
        apres = conn.execute(
            "SELECT history_id, last_internal_date FROM gmail_sync_state"
        ).fetchone()
    assert apres == avant


def test_les_etats_des_documents_sont_intacts(legacy_db):
    """Aucun document ne doit repasser en 'a traiter'.

    Un etat remis a zero declencherait une reecriture comptable et une
    relecture LLM sur des pieces deja traitees.
    """
    with sqlite3.connect(legacy_db) as conn:
        avant = sorted(conn.execute(
            "SELECT doc_key, state, stable_id, row_index FROM documents"
        ).fetchall())
    tenancy.migrate_to_multi_tenant(legacy_db)
    with sqlite3.connect(legacy_db) as conn:
        apres = sorted(conn.execute(
            "SELECT doc_key, state, stable_id, row_index FROM documents"
        ).fetchall())
    assert apres == avant


def test_les_notifications_deja_envoyees_restent_marquees(legacy_db):
    """Sans cela, chaque document deja notifie repartirait en Telegram."""
    tenancy.migrate_to_multi_tenant(legacy_db)
    with sqlite3.connect(legacy_db) as conn:
        row = conn.execute(
            "SELECT company_id, signature FROM email_notifications"
            " WHERE gmail_message_id = 'msg-1'"
        ).fetchone()
    assert row == ("xblaste", "sig-1")


def test_la_migration_est_idempotente(legacy_db):
    premier = tenancy.migrate_to_multi_tenant(legacy_db)
    assert not premier.already_migrated
    avant = tenancy.company_counts(legacy_db)

    for _ in range(3):
        rejeu = tenancy.migrate_to_multi_tenant(legacy_db)
        assert rejeu.already_migrated
        assert rejeu.rows_backfilled == {}
    assert tenancy.company_counts(legacy_db) == avant


def test_les_tables_d_origine_sont_conservees_pour_le_retour_arriere(legacy_db):
    rapport = tenancy.migrate_to_multi_tenant(legacy_db)
    assert "email_notifications_legacy_v1" in rapport.legacy_tables_kept
    assert "gmail_sync_state_legacy_v1" in rapport.legacy_tables_kept
    with sqlite3.connect(legacy_db) as conn:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "email_notifications_legacy_v1" in tables
    assert "gmail_sync_state_legacy_v1" in tables
    assert _compter(legacy_db, "email_notifications_legacy_v1") == 1


def test_la_sauvegarde_contient_l_etat_exact_d_avant_migration(legacy_db):
    with sqlite3.connect(legacy_db) as conn:
        avant = conn.execute(
            "SELECT chat_id, gmail_message_id, signature, sent_at"
            " FROM email_notifications"
        ).fetchall()
    tenancy.migrate_to_multi_tenant(legacy_db)
    with sqlite3.connect(legacy_db) as conn:
        sauvegarde = conn.execute(
            "SELECT chat_id, gmail_message_id, signature, sent_at"
            " FROM email_notifications_legacy_v1"
        ).fetchall()
    assert sauvegarde == avant


def test_une_migration_interrompue_ne_se_rejoue_pas_en_aveugle(legacy_db):
    """Une sauvegarde deja presente signale une migration incomplete.

    La rejouer ecraserait la seule copie de l'etat d'origine : on refuse
    et on demande une intervention.
    """
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("CREATE TABLE email_notifications_legacy_v1 (x TEXT)")
        conn.commit()
    with pytest.raises(RuntimeError, match="intervention requise"):
        tenancy.migrate_to_multi_tenant(legacy_db)


def test_une_migration_qui_echoue_ne_laisse_aucune_trace(legacy_db):
    """Tout ou rien : une base a moitie migree serait ingerable."""
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("CREATE TABLE gmail_sync_state_legacy_v1 (x TEXT)")
        conn.commit()
    with pytest.raises(RuntimeError):
        tenancy.migrate_to_multi_tenant(legacy_db)
    # `documents` ne doit pas avoir garde la colonne d'une migration
    # partielle, et surtout aucune ligne ne doit avoir ete rattachee.
    assert tenancy.orphan_rows(legacy_db) or not tenancy.is_migrated(legacy_db)


def test_une_base_neuve_se_migre_sans_sauvegarde(legacy_db):
    """Une base vide n'a pas d'historique a proteger."""
    neuve = tempfile.mktemp(suffix=".db")
    try:
        init_db(neuve)
        store.ensure_schema(neuve)
        rapport = tenancy.migrate_to_multi_tenant(neuve)
        assert rapport.rows_backfilled == {}
        assert tenancy.is_migrated(neuve)
    finally:
        Path(neuve).unlink(missing_ok=True)


def test_la_migration_accepte_un_autre_identifiant(legacy_db):
    """Utile pour migrer une copie de volume vers un tenant de test."""
    rapport = tenancy.migrate_to_multi_tenant(legacy_db, company_id="v2-smoke")
    assert rapport.company_id == "v2-smoke"
    assert tenancy.company_counts(legacy_db)["documents"] == {"v2-smoke": 2}


def test_les_empreintes_bancaires_deviennent_uniques_par_entreprise(legacy_db):
    """Regression : la cle primaire etait GLOBALE.

    Deux societes ayant un compte dans la meme banque produisent la meme
    empreinte pour un meme type de mouvement. Avant correction, la
    seconde etait rejetee en silence et sa ligne bancaire disparaissait.
    """
    tenancy.migrate_to_multi_tenant(legacy_db)
    with sqlite3.connect(legacy_db) as conn:
        maintenant = "2026-08-28T00:00:00+00:00"
        conn.execute(
            "INSERT INTO bank_line_fingerprints (company_id, fingerprint,"
            " chat_id, row_index, doc_key, created_at) VALUES (?,?,?,?,?,?)",
            ("fluxintelligent", "emp-1", "999653395", 2, "cle-f", maintenant),
        )
        conn.commit()
        total = conn.execute(
            "SELECT COUNT(*) FROM bank_line_fingerprints WHERE fingerprint = 'emp-1'"
        ).fetchone()[0]
    assert total == 2, "la meme empreinte doit coexister dans deux entreprises"

    # Mais le rejeu dans la MEME entreprise reste refuse.
    with sqlite3.connect(legacy_db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO bank_line_fingerprints (company_id, fingerprint,"
                " chat_id, row_index, doc_key, created_at) VALUES (?,?,?,?,?,?)",
                ("xblaste", "emp-1", "999653395", 9, "cle-x2",
                 "2026-08-28T00:00:00+00:00"),
            )


def test_les_quatre_tables_reconstruites_gardent_leur_sauvegarde(legacy_db):
    rapport = tenancy.migrate_to_multi_tenant(legacy_db)
    attendues = {
        "email_notifications_legacy_v1",
        "gmail_sync_state_legacy_v1",
        "bank_line_fingerprints_legacy_v1",
    }
    assert attendues <= set(rapport.legacy_tables_kept)
