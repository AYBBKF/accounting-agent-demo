"""Idempotence durable a travers un rollback de digest.

La deduplication vivait dans le code : un digest revenu en arriere
n'execute pas le code du digest suivant. Ces tests prouvent que la cle
d'evenement canonique (entreprise, chat, email, empreinte, membre) est
desormais imposee par la BASE elle-meme - l'index unique vit dans le
fichier SQLite du volume et s'applique a n'importe quel digest qui
l'ouvre, y compris l'ancien. Principe d'architecture observe chez
Accounted (contraintes en base, pieces immuables) et paperless-ngx
(deduplication par empreinte) ; aucun code AGPL/GPL n'a ete copie.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from app import doc_store as store
from app import ledger
from app import tenancy
from app.db import init_db


@pytest.fixture
def db_path():
    chemin = tempfile.mktemp(suffix=".db")
    init_db(chemin)
    store.ensure_schema(chemin)
    tenancy.migrate_to_multi_tenant(chemin)
    yield chemin
    Path(chemin).unlink(missing_ok=True)


def _insert_document(conn, *, doc_key, company="xblaste", chat="999653395",
                     message="msg-1", sha="a" * 64, member=None,
                     filename="facture.pdf"):
    conn.execute(
        "INSERT INTO documents (doc_key, chat_id, gmail_message_id,"
        " attachment_id, file_sha256, filename, member_path, state,"
        " company_id, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (doc_key, chat, message, "att-1", sha, filename, member,
         "COMPLETED", company),
    )


def test_la_migration_pose_l_index_d_evenement_unique(db_path):
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index'"
            " AND name='uniq_documents_evenement'"
        ).fetchone()
    assert row is not None, "l'index canonique doit exister apres migration"


def test_le_meme_evenement_est_refuse_par_la_base_meme_avec_une_autre_cle(db_path):
    """Le scenario du rollback : une version differente du code calcule
    une AUTRE cle applicative pour la MEME piece. Sans la contrainte, la
    piece serait recomptabilisee ; avec, la base refuse."""
    with sqlite3.connect(db_path) as conn:
        _insert_document(conn, doc_key="cle-du-digest-vert")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_document(conn, doc_key="cle-de-l-ancien-digest")


def test_sans_la_contrainte_le_doublon_passerait(db_path):
    """Preuve que le test precedent discrimine reellement : l'index
    retire (etat d'avant le correctif), le second evenement s'insere
    sans erreur - c'est exactement la faille corrigee."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX uniq_documents_evenement")
        _insert_document(conn, doc_key="k1")
        _insert_document(conn, doc_key="k2")  # ne leve PAS : la faille
        total = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE gmail_message_id='msg-1'"
        ).fetchone()[0]
    assert total == 2


def test_la_contrainte_s_applique_a_un_insert_brut_comme_l_ancien_digest(db_path):
    """L'ancien digest n'appelle aucun code nouveau : un INSERT brut via
    sqlite3 (ce que fait son doc_store) est bloque par le fichier de la
    base lui-meme."""
    with sqlite3.connect(db_path) as conn:
        _insert_document(conn, doc_key="k-green")
    autre = sqlite3.connect(db_path)  # connexion independante, code ancien
    with pytest.raises(sqlite3.IntegrityError):
        _insert_document(autre, doc_key="k-blue")
    autre.close()


def test_deux_membres_distincts_d_un_zip_ne_sont_pas_confondus(db_path):
    """Deux fichiers aux MEMES octets dans le meme email (deux membres
    d'un ZIP) restent deux documents : la cle canonique inclut le membre."""
    with sqlite3.connect(db_path) as conn:
        _insert_document(conn, doc_key="m1", member="dossier/a.pdf",
                         filename="a.pdf")
        _insert_document(conn, doc_key="m2", member="dossier/b.pdf",
                         filename="b.pdf")
        total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert total == 2


def test_deux_entreprises_gardent_chacune_leur_evenement(db_path):
    with sqlite3.connect(db_path) as conn:
        _insert_document(conn, doc_key="x1", company="xblaste")
        _insert_document(conn, doc_key="s1", company="v2-smoke")
        total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert total == 2


def test_des_doublons_historiques_ne_bloquent_pas_le_demarrage(db_path):
    """Un volume de production peut porter des doublons anterieurs au
    correctif de cle stable : la pose de la contrainte echoue alors SANS
    bloquer (False), et la migration reste rejouable."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX uniq_documents_evenement")
        _insert_document(conn, doc_key="h1")
        _insert_document(conn, doc_key="h2")  # doublon historique
    assert tenancy.ensure_event_uniqueness(db_path) is False
    rapport = tenancy.migrate_to_multi_tenant(db_path)  # rejouable
    assert rapport.already_migrated is True


def test_le_plan_de_comptes_par_defaut_est_exactement_celui_du_classeur():
    """01_PARAMETRES du classeur XBLASTE declare six comptes et aucun
    compte bancaire : le defaut de l'agent doit etre EXACTEMENT cela.
    Banque et frais n'existent que via account_mapping, jamais inventes."""
    attendus = {
        "client": ("3421", "Client"),
        "fournisseur": ("4411", "Fournisseur"),
        "vente": ("7111", "Vente"),
        "achat": ("6111", "Achat"),
        "tva_collectee": ("4455", "TVA collectée"),
        "tva_deductible": ("3455", "TVA déductible"),
    }
    assert ledger.TEMPLATE_ACCOUNTS == attendus
    assert ledger.AccountMapping().missing("banque", "frais_bancaires") == (
        "banque", "frais_bancaires",
    )
