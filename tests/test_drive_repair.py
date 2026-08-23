"""Recette de la reparation des archives Drive.

Le defaut repare ici : chaque PDF membre d'un ZIP avait ete archive avec le
contenu de l'ARCHIVE PARENTE. Les tests ne se contentent pas d'un code de
retour : ils relisent le contenu REELLEMENT stocke dans le faux Drive.

Aucun appel reseau : Gmail, le classeur, Drive et Calendar sont simules.
"""
import hashlib

import pytest

from app import doc_store as store
from app import doc_vault as vault
from app import drive_repair
from app.attachments import sha256_of
from app.db import init_db
from app.doc_extract import extract_document
from test_mail_worker import ACHAT, CHAT_ID, pdf_bytes, text_of, zip_of
from test_validation_policy import (
    ANOMALIE,
    PACK_MEMBERS,
    SANS_ICE,
    VENTE,
    RotatingGmail,
    send_pack,
)
from workbook_fake import FakeWorkbook

COMPTABLES = (
    "04_FACTURES_VENTES", "05_FACTURES_ACHATS", "06_RELEVE_BANCAIRE",
    "17_AVOIRS", "18_DOCUMENTS_COMMERCIAUX", "19_ECHEANCES_A_PAYER",
)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "demo.db")
    init_db(path)
    store.ensure_schema(path)
    return path


@pytest.fixture
def registry(monkeypatch):
    import app.doc_pipeline as module

    table = {}
    for name in (ACHAT, ANOMALIE, SANS_ICE, VENTE):
        table[pdf_bytes(name)] = text_of(name)

    def fake_read(content, company="X BLASTE", ocr=True):
        if content not in table:
            raise ValueError("PDF illisible")
        return extract_document([table[content]], company=company)

    monkeypatch.setattr(module, "extract_from_pdf_bytes", fake_read)
    return table


def broken_archives(worker):
    """Reproduit l'etat de production : chaque archive porte le ZIP entier.

    C'est exactement ce que faisait UPLOAD_FROM_URL avec l'URL de la piece
    jointe parente.
    """
    archive = zip_of(PACK_MEMBERS)
    for file_id, stored in worker.workbook.drive_files.items():
        stored["content"] = archive
    return archive


@pytest.fixture
def worker(db_path, registry):
    w = RotatingGmail(FakeWorkbook(), db_path)
    # Le faux Drive lit les octets deposes : les deux doubles partagent le
    # meme registre, sans toucher au worker de test commun.
    w.uploaded = w.workbook.uploaded_content
    send_pack(w)
    w.process_once()
    broken_archives(w)
    # La migration n'a pas encore tourne pour ces documents.
    for row in drive_repair.list_zip_archives(db_path, CHAT_ID):
        drive_repair.reset_repaired(db_path, row["doc_key"])
    drive_repair.set_migration(
        db_path, drive_repair.MIGRATION_KEY, drive_repair.MIGRATION_VERSION, ""
    )
    w._startup_done = False
    return w


def snapshot_comptable(worker):
    return {tab: len(worker.workbook.rows(tab)) for tab in COMPTABLES}


# === 1. vraie extraction d'un membre, contenu %PDF-, hash et taille ======

def test_the_repair_restores_each_documents_own_bytes(worker, db_path):
    report = drive_repair.run(worker)

    assert report["failed"] == []
    assert len(report["repaired"]) == 4
    for entry in report["repaired"]:
        stored = worker.workbook.drive_files[entry["nouveau_id"]]
        assert stored["content"].startswith(b"%PDF-")
        assert stored["mimeType"] == "application/pdf"
        assert stored["name"].endswith(".pdf")
        assert len(stored["content"]) == entry["taille"]
        assert sha256_of(stored["content"]) == entry["sha256"]
        assert entry["controle"] == "contenu relu"

    contenus = [
        worker.workbook.drive_files[e["nouveau_id"]]["content"]
        for e in report["repaired"]
    ]
    assert len(set(contenus)) == 4, "les douze fichiers doivent etre distincts"
    assert zip_of(PACK_MEMBERS) not in contenus


def test_the_repair_reads_the_member_out_of_the_parent_zip_when_the_vault_is_empty(
    worker, db_path
):
    for row in drive_repair.list_zip_archives(db_path, CHAT_ID):
        vault.discard(db_path, CHAT_ID, row["doc_key"])

    before = len(worker.gmail_calls)
    report = drive_repair.run(worker)

    assert report["failed"] == []
    assert len(worker.gmail_calls) > before, "le ZIP parent aurait du etre retelecharge"
    for entry in report["repaired"]:
        assert worker.workbook.drive_files[entry["nouveau_id"]]["content"].startswith(b"%PDF-")


def test_a_member_whose_hash_changed_is_refused_and_leaves_the_old_file_alone(
    worker, db_path
):
    rows = drive_repair.list_zip_archives(db_path, CHAT_ID)
    cible = rows[0]
    vault.discard(db_path, CHAT_ID, cible["doc_key"])
    altere = dict(PACK_MEMBERS)
    chemin = str(cible["member_path"])
    altere[chemin] = pdf_bytes(chemin.split("/")[-1][:-4], "modifie")
    worker.blobs["m-pack-att-1"] = zip_of(altere)

    report = drive_repair.run(worker)

    echecs = [e["doc_key"] for e in report["failed"]]
    assert cible["doc_key"][:12] in echecs
    apres = store.get_document(db_path, cible["doc_key"])
    assert not apres["archive_repaired"]


# === 2. aucune ecriture comptable, aucune ligne de journal creee =========

def test_the_repair_never_touches_an_accounting_tab(worker, db_path):
    avant = snapshot_comptable(worker)
    journal_avant = len(worker.workbook.rows("14_IMPORTS_LOG"))
    tiers_avant = len(worker.workbook.rows("03_FOURNISSEURS"))
    lignes_avant = len(worker.workbook.rows("16_LIGNES_FACTURES"))
    evenements = len(worker.workbook.events)
    deja = len(worker.workbook.calls)

    drive_repair.run(worker)

    assert snapshot_comptable(worker) == avant
    assert len(worker.workbook.rows("14_IMPORTS_LOG")) == journal_avant
    assert len(worker.workbook.rows("03_FOURNISSEURS")) == tiers_avant
    assert len(worker.workbook.rows("16_LIGNES_FACTURES")) == lignes_avant
    assert len(worker.workbook.events) == evenements
    ecrits = {
        args["range"].split("!")[0]
        for slug, args in worker.workbook.calls[deja:]
        if slug == "GOOGLESHEETS_VALUES_UPDATE"
    }
    assert not (ecrits & set(COMPTABLES))
    assert ecrits <= {"14_IMPORTS_LOG"}, ecrits


def test_the_repair_updates_the_existing_log_row_in_place(worker, db_path):
    journal_avant = [list(r) for r in worker.workbook.rows("14_IMPORTS_LOG")]

    report = drive_repair.run(worker)

    journal_apres = worker.workbook.rows("14_IMPORTS_LOG")
    assert len(journal_apres) == len(journal_avant), "aucune ligne ne doit etre creee"
    for entry in report["repaired"]:
        ligne = entry["journal_ligne"]
        assert ligne, "le lien du journal aurait du etre corrige"
        detail = journal_apres[ligne - 2][5]
        assert entry["nouveau_id"] in detail
        assert entry["ancien_id"] not in detail


# === 3. classement : valide -> categorie, ambigu -> A verifier ===========

def test_a_validated_document_lands_in_its_category_folder(worker, db_path):
    pending = [
        r for r in drive_repair.list_zip_archives(db_path, CHAT_ID)
        if r["state"] == store.NEEDS_REVIEW
    ][0]
    worker.confirm(pending["doc_key"][:24])
    for row in drive_repair.list_zip_archives(db_path, CHAT_ID):
        drive_repair.reset_repaired(db_path, row["doc_key"])
    drive_repair.set_migration(
        db_path, drive_repair.MIGRATION_KEY, drive_repair.MIGRATION_VERSION, ""
    )

    report = drive_repair.run(worker)

    repare = next(
        e for e in report["repaired"] if e["doc_key"] == pending["doc_key"][:12]
    )
    assert repare["dossier"].startswith("Factures achats/")
    assert "A verifier" not in repare["dossier"]


def test_a_document_still_awaiting_a_decision_stays_in_the_review_folder(worker, db_path):
    en_attente = {
        r["doc_key"][:12] for r in drive_repair.list_zip_archives(db_path, CHAT_ID)
        if r["state"] == store.NEEDS_REVIEW
    }
    assert en_attente

    report = drive_repair.run(worker)

    for entry in report["repaired"]:
        if entry["doc_key"] in en_attente:
            assert entry["dossier"].startswith("A verifier/")
    assert snapshot_comptable(worker)["05_FACTURES_ACHATS"] == len(
        worker.workbook.rows("05_FACTURES_ACHATS")
    )


# === 4. quarantaine : jamais avant que les vrais PDF soient verifies =====

def test_the_old_files_go_to_quarantine_only_after_every_pdf_is_verified(worker, db_path):
    report = drive_repair.run(worker)

    quarantaine = worker.workbook.folders.get(
        (worker.workbook.folders[("", "XBLASTE - Factures")], drive_repair.QUARANTINE_FOLDER)
    )
    assert quarantaine
    anciens = {e["ancien_id"] for e in report["repaired"]}
    deplaces = {e["id"] for e in report["quarantined"] if e["etat"] == "deplace"}
    assert anciens == deplaces
    for ancien in anciens:
        assert worker.workbook.drive_parents[ancien] == quarantaine


def test_a_failed_upload_keeps_every_old_file_where_it_is(worker, db_path):
    rows = drive_repair.list_zip_archives(db_path, CHAT_ID)
    worker.workbook.upload_failures.add(str(rows[0]["filename"]))
    parents_avant = dict(worker.workbook.drive_parents)

    report = drive_repair.run(worker)

    assert report["failed"], "un depot devait echouer"
    assert report["quarantined"] == [], "rien ne part en quarantaine tant qu'il reste un echec"
    for row in rows:
        ancien = drive_repair.drive_file_id(str(row["drive_link"]))
        assert worker.workbook.drive_parents[ancien] == parents_avant[ancien]


def test_the_run_resumes_after_a_failure_without_duplicating_anything(worker, db_path):
    rows = drive_repair.list_zip_archives(db_path, CHAT_ID)
    worker.workbook.upload_failures.add(str(rows[0]["filename"]))
    premier = drive_repair.run(worker)
    deposes = len(worker.workbook.uploads)

    worker.workbook.upload_failures.clear()
    second = drive_repair.run(worker)

    # Seul le document en echec est redepose.
    assert len(worker.workbook.uploads) == deposes + 1
    assert second["failed"] == []
    assert len(second["repaired"]) == 1
    assert len(second["quarantined"]) == len(rows)
    journal = worker.workbook.rows("14_IMPORTS_LOG")
    assert len(journal) == len(worker.workbook.rows("14_IMPORTS_LOG"))
    assert len(premier["repaired"]) + len(second["repaired"]) == len(rows)


# === 5. idempotence et reprise apres redemarrage =========================

def test_running_the_migration_twice_changes_nothing_the_second_time(worker, db_path):
    drive_repair.run(worker)
    deposes = len(worker.workbook.uploads)
    liens = {
        r["doc_key"]: r["drive_link"] for r in drive_repair.list_zip_archives(db_path, CHAT_ID)
    }
    journal = [list(r) for r in worker.workbook.rows("14_IMPORTS_LOG")]

    second = drive_repair.run(worker)

    assert second == {"skipped": True, "reason": "deja executee"}
    assert len(worker.workbook.uploads) == deposes
    assert {
        r["doc_key"]: r["drive_link"] for r in drive_repair.list_zip_archives(db_path, CHAT_ID)
    } == liens
    assert [list(r) for r in worker.workbook.rows("14_IMPORTS_LOG")] == journal


def test_the_migration_survives_a_restart_and_does_not_start_over(worker, db_path):
    drive_repair.run(worker)
    deposes = len(worker.workbook.uploads)

    # Nouveau processus : seul le volume (base + coffre) subsiste.
    redemarre = RotatingGmail(worker.workbook, db_path)
    redemarre.messages = worker.messages
    redemarre.blobs = worker.blobs
    apres = drive_repair.run(redemarre)

    assert apres.get("skipped") is True
    assert len(worker.workbook.uploads) == deposes


def test_the_marker_is_versioned_so_a_later_fix_can_be_replayed(db_path):
    drive_repair.set_migration(db_path, "essai", 1, "done")
    assert drive_repair.migration_state(db_path, "essai", 1) == "done"
    assert drive_repair.migration_state(db_path, "essai", 2) == ""


def test_a_backup_of_the_database_is_written_before_anything_is_touched(worker, db_path):
    from pathlib import Path

    report = drive_repair.run(worker)

    sauvegarde = Path(report["backup"])
    assert sauvegarde.exists() and sauvegarde.stat().st_size > 0
    etat = Path(report["snapshot"])
    assert etat.exists()
    import json

    documents = json.loads(etat.read_text(encoding="utf-8"))
    assert len(documents) == len(drive_repair.list_zip_archives(db_path, CHAT_ID))
    assert all("drive_link" in d and "member_path" in d for d in documents)


# === 6. Calendar en lecture seule =======================================

def test_the_calendar_check_reads_the_event_and_creates_nothing(worker):
    worker.workbook.calendar_events["evt-1"] = {
        "id": "evt-1",
        "summary": "Avis de penalite PEN-2026-044 - 450 MAD",
        "start": {"dateTime": "2026-08-31T09:00:00+01:00", "timeZone": "Africa/Casablanca"},
        "status": "confirmed",
        "organizer": {"email": "compta@example.ma"},
    }
    avant = len(worker.workbook.events)

    found = drive_repair.read_calendar_event(worker, "evt-1")

    assert found["found"] and found["id"] == "evt-1"
    assert "PEN-2026-044" in found["summary"]
    assert found["start"].startswith("2026-08-31")
    assert found["calendar"] == "compta@example.ma"
    assert len(worker.workbook.events) == avant, "aucun evenement ne doit etre cree"


def test_a_missing_calendar_event_is_reported_not_recreated(worker):
    avant = len(worker.workbook.events)
    found = drive_repair.read_calendar_event(worker, "evt-absent")
    assert found["found"] is False
    assert len(worker.workbook.events) == avant
