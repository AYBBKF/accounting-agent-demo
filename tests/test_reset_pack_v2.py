"""Retour en validation des cinq documents du Pack V2.

Ces cinq pieces avaient ete comptabilisees automatiquement alors que leur
lecture etait ambigue. Ce module prouve que la tache de remise en attente :

  * sauvegarde la base AVANT toute ecriture ;
  * ne touche QUE les cinq numeros nommement identifies ;
  * conserve l'empreinte du fichier et le lien Drive de chaque fiche ;
  * remet les compteurs de notification a zero pour que chaque document
    redemande son arbitrage UNE seule fois ;
  * ne s'execute qu'une fois.
"""
import pytest

from app import doc_store as store
from app import drive_repair
from app.db import init_db

CHAT_ID = 999653395
MESSAGE = "1a02a81e3859b111"


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


def fiche(db_path, cle, numero, *, sha, state=store.COMPLETED, drive="", **extra):
    store.claim_document(
        db_path, cle, CHAT_ID, gmail_message_id=MESSAGE,
        attachment_id="volatil", file_sha256=sha, filename=f"{numero}.pdf",
    )
    store.update_document(
        db_path, cle, state=state, numero=numero, drive_link=drive, **extra
    )


def peupler(db_path):
    for index, numero in enumerate(drive_repair.RESET_V2_NUMEROS):
        fiche(
            db_path, f"{index:02d}" + "a" * 62, numero,
            sha=f"{index:02d}" * 32,
            drive=f"https://drive.google.com/file/d/file{index}/view",
            stable_id=f"FA-2026-0{20 + index}", tab="05_FACTURES_ACHATS",
            row_index=21 + index, last_notified_state="completed",
            telegram_message_id=555 + index,
        )
    # Un document etranger a l'operation, qui ne doit jamais bouger.
    fiche(db_path, "ff" * 32, "FAC-TEST-2026-002", sha="ee" * 32,
          stable_id="FA-2026-001", tab="05_FACTURES_ACHATS", row_index=3)
    store.mark_notified(db_path, "ff" * 32, "completed")


def test_the_five_documents_go_back_to_validation(db_path):
    peupler(db_path)
    rapport = drive_repair.reset_pack_v2_documents(FauxWorker(db_path))

    assert len(rapport["remises"]) == 5
    assert not rapport["absentes"]
    assert all(entree["conforme"] for entree in rapport["remises"])
    for entree in rapport["remises"]:
        assert entree["apres"]["state"] == store.NEEDS_REVIEW
        assert not entree["apres"]["stable_id"]
        assert int(entree["apres"]["row_index"] or 0) == 0


def test_the_file_fingerprint_and_the_drive_link_survive(db_path):
    peupler(db_path)
    drive_repair.reset_pack_v2_documents(FauxWorker(db_path))
    for index, _ in enumerate(drive_repair.RESET_V2_NUMEROS):
        relu = store.get_document(db_path, f"{index:02d}" + "a" * 62)
        assert relu["file_sha256"] == f"{index:02d}" * 32
        assert relu["drive_link"] == f"https://drive.google.com/file/d/file{index}/view"


def test_each_document_will_ask_for_its_decision_again(db_path):
    peupler(db_path)
    drive_repair.reset_pack_v2_documents(FauxWorker(db_path))
    for index, _ in enumerate(drive_repair.RESET_V2_NUMEROS):
        relu = store.get_document(db_path, f"{index:02d}" + "a" * 62)
        # Sans remise a zero, le worker considererait la demande de
        # validation comme deja envoyee et le document resterait muet.
        assert not (relu["last_notified_state"] or "")
        assert not (relu["validation_notification_sent_at"] or "")
        assert int(relu["telegram_message_id"] or 0) == 0


def test_no_other_document_is_touched(db_path):
    peupler(db_path)
    drive_repair.reset_pack_v2_documents(FauxWorker(db_path))
    etranger = store.get_document(db_path, "ff" * 32)
    assert etranger["state"] == store.COMPLETED
    assert etranger["stable_id"] == "FA-2026-001"
    assert int(etranger["row_index"]) == 3
    assert etranger["last_notified_state"] == "completed"


def test_the_task_backs_up_the_database_first(db_path):
    peupler(db_path)
    rapport = drive_repair.reset_pack_v2_documents(FauxWorker(db_path))
    from pathlib import Path

    assert Path(rapport["backup"]).exists()


def test_the_task_runs_only_once(db_path):
    peupler(db_path)
    drive_repair.reset_pack_v2_documents(FauxWorker(db_path))
    second = drive_repair.reset_pack_v2_documents(FauxWorker(db_path))
    assert second.get("skipped") is True


def test_a_missing_document_is_reported_not_invented(db_path):
    # Base vide : rien a remettre, et surtout aucune fiche creee.
    rapport = drive_repair.reset_pack_v2_documents(FauxWorker(db_path))
    assert sorted(rapport["absentes"]) == sorted(drive_repair.RESET_V2_NUMEROS)
    assert not rapport["remises"]
    assert store.list_documents(db_path, CHAT_ID) == []
