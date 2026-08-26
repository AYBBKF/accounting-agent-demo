"""Nettoyage des fiches parasites du 23/08/2026.

Trois enregistrements ont ete crees par erreur pour deux documents qui
existaient deja. Ce module prouve que la tache de nettoyage :

  * sauvegarde la base et l'inventaire AVANT de supprimer quoi que ce soit ;
  * ne supprime que les trois cles nommement identifiees ;
  * ne supprime JAMAIS une fiche qui est le seul enregistrement de son
    document, meme si sa cle figure dans la liste ;
  * ne s'execute qu'une fois.
"""
import json
from pathlib import Path

import pytest

from app import doc_store as store
from app import drive_repair
from app.db import init_db

CHAT_ID = 42
MESSAGE = "gmail-message-fixture"
SHA_EXPORT = "aa" * 32
SHA_IMPORT = "bb" * 32


class FauxWorker:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._chat_id = CHAT_ID


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "demo.db")
    init_db(path)
    store.ensure_schema(path)
    return path


def claim(db_path, cle, sha, *, drive="", log_row=0, state=store.NEEDS_REVIEW):
    store.claim_document(
        db_path, cle, CHAT_ID, gmail_message_id=MESSAGE,
        attachment_id="volatil", file_sha256=sha, filename="doc.pdf",
    )
    store.update_document(
        db_path, cle, state=state, drive_link=drive, log_row=log_row,
    )


def test_only_the_three_parasites_are_removed(db_path):
    # Les deux fiches canoniques, creees en premier.
    claim(db_path, "d5331e88d32b" + "0" * 52, SHA_EXPORT,
          drive="https://drive.google.com/file/d/test-exp-original/view", log_row=10)
    claim(db_path, "cfa6d9b526eb" + "0" * 52, SHA_IMPORT,
          drive="https://drive.google.com/file/d/test-ci-original/view", log_row=9)
    # Les trois parasites, creees ensuite.
    claim(db_path, "29dee48b317c" + "0" * 52, SHA_EXPORT,
          drive="https://drive.google.com/file/d/test-exp-orphan/view", log_row=15)
    claim(db_path, "4b6ea8b8a6e3" + "0" * 52, SHA_IMPORT,
          drive="https://drive.google.com/file/d/test-ci-duplicate/view", log_row=16)
    claim(db_path, "af8348a31d13" + "0" * 52, SHA_EXPORT,
          drive="https://drive.google.com/file/d/test-exp-duplicate/view", log_row=17)
    # Un document sans rapport, qui ne doit pas bouger.
    claim(db_path, "ffffffffffff" + "0" * 52, "cc" * 32, state=store.COMPLETED)

    rapport = drive_repair.cleanup_duplicates(FauxWorker(db_path))

    assert len(rapport["supprimes"]) == 3
    assert rapport["conserves"] == []
    assert Path(rapport["backup"]).exists()
    inventaire = json.loads(Path(rapport["inventaire"]).read_text(encoding="utf-8"))
    assert len(inventaire) == 6, "l'inventaire est pris AVANT suppression"

    restant = {r["doc_key"][:12] for r in store.list_documents(db_path, CHAT_ID)}
    assert restant == {"d5331e88d32b", "cfa6d9b526eb", "ffffffffffff"}


def test_a_parasite_key_that_is_the_only_record_is_kept(db_path):
    """Si la fiche canonique a disparu, le 'parasite' EST le document."""
    claim(db_path, "4b6ea8b8a6e3" + "0" * 52, SHA_IMPORT,
          drive="https://drive.google.com/file/d/test-ci-duplicate/view", log_row=16)

    rapport = drive_repair.cleanup_duplicates(FauxWorker(db_path))

    assert rapport["supprimes"] == []
    assert len(rapport["conserves"]) == 1
    assert rapport["conserves"][0]["cle"] == "4b6ea8b8a6e3"
    assert store.get_document(db_path, "4b6ea8b8a6e3" + "0" * 52) is not None


def test_the_cleanup_runs_only_once(db_path):
    claim(db_path, "cfa6d9b526eb" + "0" * 52, SHA_IMPORT, log_row=9)
    claim(db_path, "4b6ea8b8a6e3" + "0" * 52, SHA_IMPORT, log_row=16)

    premier = drive_repair.cleanup_duplicates(FauxWorker(db_path))
    assert len(premier["supprimes"]) == 1

    second = drive_repair.cleanup_duplicates(FauxWorker(db_path))
    assert second == {"skipped": True, "reason": "deja executee"}
    assert len(store.list_documents(db_path, CHAT_ID)) == 1


def test_missing_keys_are_reported_not_invented(db_path):
    claim(db_path, "cfa6d9b526eb" + "0" * 52, SHA_IMPORT, log_row=9)

    rapport = drive_repair.cleanup_duplicates(FauxWorker(db_path))

    assert rapport["supprimes"] == []
    assert sorted(rapport["absents"]) == sorted(drive_repair.DUPLICATE_KEY_PREFIXES)
    assert store.get_document(db_path, "cfa6d9b526eb" + "0" * 52) is not None


# === rattachement des fiches vivantes a leur ligne d'origine ==============

def test_restore_only_touches_log_row_and_drive_link(db_path, monkeypatch):
    """Option A : deux champs corriges, tout le reste intact."""
    monkeypatch.setenv(
        "RESTORE_CANONICAL_DRIVE_ID_CI_2026_045", "canonical-import-file"
    )
    cle = "4b6ea8b8a6e3" + "0" * 52
    claim(db_path, cle, SHA_IMPORT,
          drive="https://drive.google.com/file/d/test-ci-duplicate/view", log_row=16)
    store.update_document(db_path, cle, numero="CI-2026-045", member_path="pack/ci.pdf")
    store.mark_notified(db_path, cle, "waiting_validation", telegram_message_id=7)
    avant = store.get_document(db_path, cle)

    rapport = drive_repair.restore_canonical_links(FauxWorker(db_path))

    corrigee = next(e for e in rapport["corrigees"] if e["cle"] == "4b6ea8b8a6e3")
    assert corrigee["conforme"] is True
    assert "UPDATE documents SET log_row = 9" in corrigee["sql"]

    apres = store.get_document(db_path, cle)
    assert apres["log_row"] == 9
    assert apres["drive_link"].endswith("canonical-import-file/view")
    # Tout le reste est preserve, champ par champ.
    for champ in (
        "doc_key", "state", "file_sha256", "member_path", "numero",
        "telegram_message_id", "last_notified_state",
        "validation_notification_sent_at", "chat_id", "gmail_message_id",
    ):
        assert apres[champ] == avant[champ], champ


def test_restore_refuses_a_record_whose_number_does_not_match(db_path):
    """Un garde-fou : on ne rattache pas une fiche qui n'est pas la bonne."""
    cle = "af8348a31d13" + "0" * 52
    claim(db_path, cle, SHA_EXPORT, drive="https://drive.google.com/file/d/x/view",
          log_row=17)
    store.update_document(db_path, cle, numero="AUTRE-CHOSE")

    rapport = drive_repair.restore_canonical_links(FauxWorker(db_path))

    assert rapport["corrigees"] == []
    ignoree = next(e for e in rapport["ignorees"] if e["cle"] == "af8348a31d13")
    assert "numero inattendu" in ignoree["motif"]
    assert store.get_document(db_path, cle)["log_row"] == 17


def test_restore_runs_only_once(db_path, monkeypatch):
    monkeypatch.setenv(
        "RESTORE_CANONICAL_DRIVE_ID_CI_2026_045", "canonical-import-file"
    )
    cle = "4b6ea8b8a6e3" + "0" * 52
    claim(db_path, cle, SHA_IMPORT, log_row=16)
    store.update_document(db_path, cle, numero="CI-2026-045")

    premier = drive_repair.restore_canonical_links(FauxWorker(db_path))
    assert len(premier["corrigees"]) == 1

    store.update_document(db_path, cle, log_row=99)
    second = drive_repair.restore_canonical_links(FauxWorker(db_path))
    assert second == {"skipped": True, "reason": "deja executee"}
    assert store.get_document(db_path, cle)["log_row"] == 99


def test_restore_does_not_publish_or_invent_a_missing_drive_id(db_path):
    cle = "4b6ea8b8a6e3" + "0" * 52
    claim(db_path, cle, SHA_IMPORT, log_row=16)
    store.update_document(db_path, cle, numero="CI-2026-045")

    rapport = drive_repair.restore_canonical_links(FauxWorker(db_path))

    assert rapport["corrigees"] == []
    assert "configuration absente" in rapport["ignorees"][0]["motif"]
    assert drive_repair.migration_state(
        db_path, drive_repair.RESTORE_KEY, drive_repair.RESTORE_VERSION
    ) == ""
