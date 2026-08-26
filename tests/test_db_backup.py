"""La sauvegarde SQLite doit etre REELLE, verifiee, et bloquante.

Ce module verifie deux choses opposees, et la seconde compte autant que
la premiere :

  - une sauvegarde saine est produite, verifiee et journalisee sans
    jamais divulguer de donnee ;
  - une sauvegarde douteuse ARRETE la migration avant toute ecriture.

Le second cas est le seul qui protege reellement : un filet qu'on ne
teste que quand il tient ne prouve rien.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app.db_backup import (
    BackupError,
    integrity_ok,
    sha256_of,
    verified_backup,
    verify_backup_file,
)


def base(tmp_path: Path, lignes: int = 50) -> str:
    chemin = tmp_path / "demo.db"
    conn = sqlite3.connect(chemin)
    conn.execute("CREATE TABLE documents (doc_key TEXT PRIMARY KEY, etat TEXT)")
    conn.executemany(
        "INSERT INTO documents VALUES (?, ?)",
        [(f"cle-{i}", "needs_review") for i in range(lignes)],
    )
    conn.commit()
    conn.close()
    return str(chemin)


# === 1. le cas nominal ====================================================

def test_a_verified_backup_reports_path_size_hash_and_integrity(tmp_path):
    rapport = verified_backup(base(tmp_path), "essai")

    copie = Path(str(rapport["path"]))
    assert copie.exists()
    assert rapport["size"] == copie.stat().st_size > 0
    assert rapport["integrity_check"] == "ok"
    assert len(str(rapport["sha256"])) == 64
    assert str(rapport["sha256"]) == sha256_of(copie)


def test_the_backup_really_contains_the_data(tmp_path):
    """Une copie vide passerait les controles de taille : on lit dedans."""
    rapport = verified_backup(base(tmp_path, lignes=50), "essai")
    conn = sqlite3.connect(f"file:{rapport['path']}?mode=ro", uri=True)
    try:
        total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        conn.close()
    assert total == 50


def test_the_file_name_is_timestamped_and_never_overwrites(tmp_path):
    chemin = base(tmp_path)
    premier = verified_backup(chemin, "essai")["path"]
    second = verified_backup(chemin, "essai")["path"]
    assert premier != second, "deux sauvegardes ne doivent jamais se recouvrir"
    assert Path(str(premier)).exists() and Path(str(second)).exists()


def test_nothing_confidential_is_logged(tmp_path, caplog):
    """Le journal ne porte qu'un chemin, une taille, une empreinte."""
    import logging
    with caplog.at_level(logging.INFO, logger="demo_bot.db_backup"):
        verified_backup(base(tmp_path), "essai")
    texte = caplog.text
    assert "integrity_check=ok" in texte
    assert "sha256=" in texte
    # Aucune donnee metier ni valeur de table ne doit fuir.
    assert "needs_review" not in texte
    assert "cle-0" not in texte


# === 2. les cas ou le filet doit refuser ==================================

def test_a_missing_source_database_is_refused(tmp_path):
    with pytest.raises(BackupError, match="introuvable"):
        verified_backup(str(tmp_path / "absente.db"), "essai")


def test_a_corrupted_backup_is_detected_and_refused(tmp_path, monkeypatch):
    """Coeur du test : integrity_check fait echouer, donc bloque."""
    import app.db_backup as module
    monkeypatch.setattr(module, "integrity_ok", lambda p: (False, "malformed"))
    with pytest.raises(BackupError, match="integrity_check=malformed"):
        verified_backup(base(tmp_path), "essai")


def test_an_empty_backup_file_is_refused(tmp_path):
    """Un fichier de taille nulle ne doit jamais passer pour un filet."""
    vide = tmp_path / "vide.db"
    vide.touch()
    with pytest.raises(BackupError, match="vide"):
        verify_backup_file(vide)


def test_a_missing_backup_file_is_refused(tmp_path):
    with pytest.raises(BackupError, match="absente"):
        verify_backup_file(tmp_path / "jamais-ecrite.db")


def test_a_file_that_is_not_a_database_is_refused(tmp_path):
    """Des octets quelconques ne sont pas une base, meme non vides."""
    faux = tmp_path / "faux.db"
    faux.write_bytes(b"ceci n'est pas une base SQLite" * 10)
    with pytest.raises(BackupError):
        verify_backup_file(faux)


def test_an_unreadable_backup_is_refused(tmp_path, monkeypatch):
    import app.db_backup as module

    def casse(path):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(module, "integrity_ok", casse)
    with pytest.raises(BackupError, match="illisible"):
        verified_backup(base(tmp_path), "essai")


def test_integrity_check_reads_the_copy_not_the_original(tmp_path):
    rapport = verified_backup(base(tmp_path), "essai")
    saine, verdict = integrity_ok(Path(str(rapport["path"])))
    assert saine and verdict == "ok"


# === 3. la migration s'arrete si le filet cede ============================

def test_the_migration_aborts_without_writing_when_the_backup_fails(
    tmp_path, monkeypatch
):
    """Aucune ecriture, ni SQLite ni Sheets, si la sauvegarde est douteuse."""
    import app.drive_repair as repair

    monkeypatch.setattr(
        repair, "verified_backup",
        lambda db, label: (_ for _ in ()).throw(BackupError("integrity_check=malformed")),
    )

    from app import doc_store as store

    chemin = str(tmp_path / "reelle.db")
    store.ensure_schema(chemin)

    class WorkerMuet:
        _db_path = chemin
        _chat_id = 1

        @property
        def pipeline(self):
            raise AssertionError("le pipeline ne doit JAMAIS etre sollicite")

    rapport = repair.migrate_needs_review_to_review_tab(WorkerMuet())

    assert rapport["skipped"] is True
    assert rapport["aborted"] is True
    assert "integrity_check=malformed" in rapport["reason"]
