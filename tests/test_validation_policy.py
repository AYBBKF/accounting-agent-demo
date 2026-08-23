"""Recette de la politique de validation et de la reprise des ZIP.

Onze points, demandes explicitement par le client. Ils tiennent en une
phrase : la comptabilite doit etre AUTOMATIQUE, et l'on ne derange le client
que lorsqu'aucune decision comptable fiable n'est possible.

Les tests 5 a 10 couvrent le defaut le plus penible constate en production :
un PDF contenu dans un ZIP n'est pas une piece jointe Gmail, et le bouton
"Valider" repondait "Piece jointe introuvable dans l'email d'origine".

Aucun appel reseau : Gmail, le classeur, Drive et Calendar sont simules.
"""
import tempfile
from pathlib import Path

import pytest

from app import doc_store as store
from app import doc_vault as vault
from app.attachments import sha256_of
from app.db import init_db
from app.doc_extract import extract_document
from app.doc_policy import ACTION_AUTO, ACTION_DUPLICATE, ACTION_REVIEW
from app.mail_worker import MailWorkerError
from test_mail_worker import (
    ACHAT,
    CHAT_ID,
    FakeMailWorker,
    pdf_bytes,
    text_of,
    zip_of,
)
from workbook_fake import FakeWorkbook

ANOMALIE = "09_FACTURE_ANOMALIE_FAC-TEST-2026-003_VALIDATION_REQUISE"
SANS_ICE = "10_FACTURE_ICE_MANQUANT_FAC-TEST-2026-004_VALIDATION_REQUISE"
VENTE = "02_FACTURE_VENTE_OK_FAC-VTE-TEST-2026-012"

PACK_MEMBERS = {
    f"pack/{ACHAT}.pdf": pdf_bytes(ACHAT),
    f"pack/{ANOMALIE}.pdf": pdf_bytes(ANOMALIE),
    f"pack/{SANS_ICE}.pdf": pdf_bytes(SANS_ICE),
    f"pack/{VENTE}.pdf": pdf_bytes(VENTE),
}


class RotatingGmail(FakeMailWorker):
    """Gmail tel qu'il se comporte REELLEMENT : les `attachmentId` changent.

    C'est la cause racine du bug de validation. Le double le reproduit pour
    que la correction soit prouvee, et non supposee : chaque lecture du
    message renvoie de nouveaux identifiants de pieces jointes, le contenu
    restant evidemment le meme.
    """

    def _gmail(self, slug: str, arguments: dict) -> dict:
        if slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID":
            message = super()._gmail(slug, arguments)
            rotated = []
            for att in message.get("attachmentList", []):
                fresh = f"{att['attachmentId']}-r{len(self.gmail_calls)}"
                self.blobs[fresh] = self.blobs[att["attachmentId"]]
                rotated.append({**att, "attachmentId": fresh})
            message["attachmentList"] = rotated
            return message
        return super()._gmail(slug, arguments)


@pytest.fixture
def db_path(tmp_path):
    """Base ET coffre isoles : le coffre vit a cote de la base."""
    path = str(tmp_path / "demo.db")
    init_db(path)
    store.ensure_schema(path)
    return path


@pytest.fixture
def registry(monkeypatch):
    """Lecture des PDF simulee ; le texte est celui des vrais PDF du client."""
    import app.doc_pipeline as module

    table: dict[bytes, str] = {}
    for name in (ACHAT, ANOMALIE, SANS_ICE, VENTE):
        table[pdf_bytes(name)] = text_of(name)

    def fake_read(content, company="X BLASTE", ocr=True):
        if content not in table:
            raise ValueError("PDF illisible")
        return extract_document([table[content]], company=company)

    monkeypatch.setattr(module, "extract_from_pdf_bytes", fake_read)
    return table


@pytest.fixture
def worker(db_path, registry):
    return RotatingGmail(FakeWorkbook(), db_path)


def send_pack(worker, message_id: str = "m-pack") -> None:
    worker.add_message(
        message_id,
        internal_date=worker.moment(),
        attachments={"Pack_test_comptable.zip": zip_of(PACK_MEMBERS)},
    )


def documents(worker) -> dict[str, dict]:
    """Documents enregistres, indexes par nom de fichier."""
    rows = store.list_by_message(worker._db_path, CHAT_ID, "m-pack")
    return {row["filename"]: row for row in rows}


# === 1. facture propre : import automatique, aucun bouton ================

def test_a_clean_invoice_is_imported_without_any_button(worker):
    worker.add_message(
        "m1", internal_date=worker.moment(),
        attachments={"achat.pdf": pdf_bytes(ACHAT)},
    )
    summary = worker.process_once()[0]

    assert summary.to_review == [], "un document propre ne doit produire aucun bouton"
    imported = summary.imported
    assert len(imported) == 1
    outcome = imported[0]
    assert outcome.action == ACTION_AUTO
    assert outcome.tab == "05_FACTURES_ACHATS" and outcome.row_index
    assert outcome.drive_link                      # archive
    assert worker.workbook.rows("14_IMPORTS_LOG")  # journal
    assert store.get_document(worker._db_path, outcome.doc_key)["state"] == store.COMPLETED


# === 2. ICE manquant seul : import automatique avec avertissement =========

def test_a_missing_ice_alone_never_asks_anything(worker):
    worker.add_message(
        "m1", internal_date=worker.moment(),
        attachments={"sans-ice.pdf": pdf_bytes(SANS_ICE)},
    )
    summary = worker.process_once()[0]

    assert summary.to_review == []
    outcome = summary.imported[0]
    assert outcome.action == ACTION_AUTO
    assert any("ICE absent" in w for w in outcome.warnings)
    created = worker.workbook.rows("03_FOURNISSEURS")[-1]
    assert created[1] == "EXPRESS SERVICE" and created[2] == "A completer"


# === 3. HT + TVA != TTC : validation reellement demandee ==================

def test_an_amount_contradiction_asks_for_a_decision(worker):
    worker.add_message(
        "m1", internal_date=worker.moment(),
        attachments={"anomalie.pdf": pdf_bytes(ANOMALIE)},
    )
    summary = worker.process_once()[0]

    assert len(summary.to_review) == 1
    outcome = summary.to_review[0]
    assert any("HT + TVA ne correspond pas au TTC" in r for r in outcome.reasons)
    assert worker.workbook.writes_to("05_FACTURES_ACHATS") == []


# === 4. document ambigu : archive dans Drive "A verifier" =================

def test_a_document_awaiting_a_decision_is_archived_and_logged(worker):
    worker.add_message(
        "m1", internal_date=worker.moment(),
        attachments={"anomalie.pdf": pdf_bytes(ANOMALIE)},
    )
    outcome = worker.process_once()[0].to_review[0]

    upload = worker.workbook.uploads[-1]
    assert upload["file_to_upload"]["name"] == "anomalie.pdf"
    folders = {name for (_parent, name) in worker.workbook.folders}
    assert "A verifier" in folders
    assert outcome.drive_link

    entry = worker.workbook.rows("14_IMPORTS_LOG")[-1]
    assert entry[4] == "Archive dans Drive / A verifier - ecriture en attente"
    assert "EN ATTENTE DE VALIDATION" in entry[5]

    # ... mais AUCUNE ecriture comptable.
    assert worker.workbook.writes_to("05_FACTURES_ACHATS") == []
    assert worker.workbook.writes_to("16_LIGNES_FACTURES") == []
    assert worker.workbook.events == []


def test_the_notification_no_longer_claims_nothing_was_written(worker):
    from app.mail_worker import build_review_message

    worker.add_message(
        "m1", internal_date=worker.moment(),
        attachments={"anomalie.pdf": pdf_bytes(ANOMALIE)},
    )
    outcome = worker.process_once()[0].to_review[0]
    message = build_review_message(outcome)

    assert "Rien n'a ete ecrit dans Sheets, Drive ni Calendar" not in message
    assert "Archive dans Drive / A verifier" in message
    assert "en attente de ta decision" in message


# === 5. validation d'un PDF contenu dans un ZIP ==========================

def test_validating_a_pdf_that_lives_inside_a_zip_works(worker):
    send_pack(worker)
    summary = worker.process_once()[0]
    pending = summary.to_review[0]
    assert pending.filename.endswith(f"{ANOMALIE}.pdf")

    row = store.get_document(worker._db_path, pending.doc_key)
    assert row["member_path"] == f"pack/{ANOMALIE}.pdf"
    assert row["parent_filename"] == "Pack_test_comptable.zip"

    reply = worker.confirm(pending.doc_key[:24])

    assert "Document valide et enregistre" in reply
    after = store.get_document(worker._db_path, pending.doc_key)
    assert after["state"] == store.COMPLETED
    assert after["tab"] == "05_FACTURES_ACHATS" and after["row_index"]


def test_the_child_pdf_is_never_looked_up_as_a_gmail_attachment(worker):
    """Regression : c'est cette recherche qui produisait le message d'erreur."""
    send_pack(worker)
    pending = worker.process_once()[0].to_review[0]

    worker.confirm(pending.doc_key[:24])   # ne doit pas lever

    # La piece jointe demandee a Gmail reste le ZIP, jamais le PDF enfant.
    demanded = [
        args.get("file_name") for slug, args in worker.gmail_calls
        if slug == "GMAIL_GET_ATTACHMENT"
    ]
    assert all(name is None or name.endswith(".zip") for name in demanded)


# === 6. validation apres redemarrage du conteneur ========================

def test_validation_still_works_after_a_container_restart(worker, db_path, registry):
    send_pack(worker)
    pending = worker.process_once()[0].to_review[0]

    # Nouveau processus : plus aucun etat en memoire. Seuls subsistent le
    # volume (base + coffre) et l'email d'origine.
    restarted = RotatingGmail(FakeWorkbook(), db_path)
    restarted.messages = worker.messages
    restarted.blobs = worker.blobs

    reply = restarted.confirm(pending.doc_key[:24])

    assert "Document valide et enregistre" in reply
    assert store.get_document(db_path, pending.doc_key)["state"] == store.COMPLETED


# === 7. fichier local manquant : recuperation du ZIP parent ==============

def test_a_missing_local_file_is_rebuilt_from_the_parent_zip(worker, db_path):
    send_pack(worker)
    pending = worker.process_once()[0].to_review[0]

    # Le volume a ete purge : le coffre est vide.
    vault.discard(db_path, CHAT_ID, pending.doc_key)
    assert vault.load(db_path, CHAT_ID, pending.doc_key) is None

    before = len(worker.gmail_calls)
    reply = worker.confirm(pending.doc_key[:24])

    assert "Document valide et enregistre" in reply
    assert len(worker.gmail_calls) > before, "le ZIP parent aurait du etre retelecharge"
    # ... et le coffre est reconstitue pour la prochaine fois.
    row = store.get_document(db_path, pending.doc_key)
    assert vault.load(db_path, CHAT_ID, pending.doc_key, row["file_sha256"]) is not None


def test_only_the_requested_member_is_extracted_from_the_archive(worker):
    from app.attachments import extract_member

    archive = zip_of(PACK_MEMBERS)
    found = extract_member(archive, f"pack/{ANOMALIE}.pdf")
    assert found == pdf_bytes(ANOMALIE)
    assert extract_member(archive, "pack/inexistant.pdf") is None


# === 8. empreinte differente : blocage securise ==========================

def test_a_changed_file_blocks_the_write_instead_of_guessing(worker, db_path):
    send_pack(worker)
    pending = worker.process_once()[0].to_review[0]
    vault.discard(db_path, CHAT_ID, pending.doc_key)

    # L'email est rejoue avec un contenu different sous le MEME chemin.
    altered = dict(PACK_MEMBERS)
    altered[f"pack/{ANOMALIE}.pdf"] = pdf_bytes(ANOMALIE, "modifie")
    worker.blobs["m-pack-att-1"] = zip_of(altered)
    before = len(worker.workbook.rows("05_FACTURES_ACHATS"))

    with pytest.raises(MailWorkerError, match="empreinte differente"):
        worker.confirm(pending.doc_key[:24])

    assert store.get_document(db_path, pending.doc_key)["state"] == store.NEEDS_REVIEW
    assert len(worker.workbook.rows("05_FACTURES_ACHATS")) == before


def test_a_corrupted_vault_file_is_ignored_not_trusted(worker, db_path):
    send_pack(worker)
    pending = worker.process_once()[0].to_review[0]
    row = store.get_document(db_path, pending.doc_key)

    vault.save(db_path, CHAT_ID, pending.doc_key, b"%PDF-corrompu")
    assert vault.load(db_path, CHAT_ID, pending.doc_key, row["file_sha256"]) is None


# === 9. double clic : une seule ecriture =================================

def test_clicking_validate_twice_writes_exactly_one_row(worker):
    send_pack(worker)
    pending = worker.process_once()[0].to_review[0]

    worker.confirm(pending.doc_key[:24])
    rows = len(worker.workbook.rows("05_FACTURES_ACHATS"))
    lines = len(worker.workbook.rows("16_LIGNES_FACTURES"))
    logs = len(worker.workbook.rows("14_IMPORTS_LOG"))

    second = worker.confirm(pending.doc_key[:24])

    assert "deja enregistre" in second
    assert len(worker.workbook.rows("05_FACTURES_ACHATS")) == rows
    assert len(worker.workbook.rows("16_LIGNES_FACTURES")) == lines
    assert len(worker.workbook.rows("14_IMPORTS_LOG")) == logs


def test_a_third_click_still_writes_nothing(worker):
    send_pack(worker)
    pending = worker.process_once()[0].to_review[0]
    worker.confirm(pending.doc_key[:24])
    rows = len(worker.workbook.rows("05_FACTURES_ACHATS"))
    worker.confirm(pending.doc_key[:24])
    worker.confirm(pending.doc_key[:24])
    assert len(worker.workbook.rows("05_FACTURES_ACHATS")) == rows


# === 10. plusieurs PDF du meme ZIP restent independants ==================

def test_every_pdf_of_one_zip_keeps_its_own_identity(worker, db_path):
    send_pack(worker)
    summary = worker.process_once()[0]

    assert len(summary.outcomes) == 4
    assert len({o.doc_key for o in summary.outcomes}) == 4

    rows = store.list_by_message(db_path, CHAT_ID, "m-pack")
    assert len({r["member_path"] for r in rows}) == 4
    assert all(r["member_path"].startswith("pack/") for r in rows)
    assert len({r["file_sha256"] for r in rows}) == 4

    # Valider l'un ne touche pas les autres.
    pending = summary.to_review[0]
    others = {o.doc_key: o.action for o in summary.outcomes if o.doc_key != pending.doc_key}
    worker.confirm(pending.doc_key[:24])
    for doc_key, action in others.items():
        state = store.get_document(db_path, doc_key)["state"]
        assert state == store.COMPLETED if action == ACTION_AUTO else state != store.COMPLETED


def test_two_members_with_the_same_name_in_different_folders_stay_distinct():
    from app.attachments import collect_documents

    archive = zip_of({
        "2025/facture.pdf": pdf_bytes(ACHAT),
        "2026/facture.pdf": pdf_bytes(VENTE),
    })
    report = collect_documents("pack.zip", archive)
    assert len(report.files) == 2
    assert {f.member_path for f in report.files} == {"2025/facture.pdf", "2026/facture.pdf"}
    assert len({f.stable_ref for f in report.files}) == 2


# === 11. aucune facture deja importee n'est dupliquee ====================

def test_replaying_the_same_email_never_writes_a_second_row(worker):
    send_pack(worker)
    worker.process_once()
    rows = len(worker.workbook.rows("05_FACTURES_ACHATS"))
    suppliers = len(worker.workbook.rows("03_FOURNISSEURS"))
    logs = len(worker.workbook.rows("14_IMPORTS_LOG"))

    worker.rewind(24)
    worker.process_once()
    worker.rewind(24)
    worker.process_once()

    assert len(worker.workbook.rows("05_FACTURES_ACHATS")) == rows
    assert len(worker.workbook.rows("03_FOURNISSEURS")) == suppliers
    assert len(worker.workbook.rows("14_IMPORTS_LOG")) == logs


def test_a_rotating_gmail_attachment_id_no_longer_creates_a_new_document(worker, db_path):
    """Le defaut de fond : Gmail renumerote ses pieces jointes a chaque appel.

    La cle d'idempotence en dependait ; chaque cycle inventait donc un
    document neuf pour la meme facture, et les boutons deja envoyes
    pointaient vers des cles mortes.
    """
    send_pack(worker)
    worker.process_once()
    keys = {r["doc_key"] for r in store.list_by_message(db_path, CHAT_ID, "m-pack")}

    worker.rewind(24)
    worker.process_once()

    again = {r["doc_key"] for r in store.list_by_message(db_path, CHAT_ID, "m-pack")}
    assert again == keys, "un identifiant Gmail volatil ne doit plus creer de doublon."


def test_retry_pending_finishes_what_is_left_without_duplicating(worker, db_path):
    send_pack(worker)
    summary = worker.process_once()[0]
    pending = summary.to_review
    rows = len(worker.workbook.rows("05_FACTURES_ACHATS"))

    outcomes = worker.retry_pending()

    assert len(outcomes) == len(pending)
    assert all(o.action == ACTION_REVIEW for o in outcomes)
    assert len(worker.workbook.rows("05_FACTURES_ACHATS")) == rows
    # Les boutons redeviennent vivants : la cle rendue existe toujours.
    for outcome in outcomes:
        assert store.find_by_key_prefix(db_path, CHAT_ID, outcome.doc_key[:24]) is not None


def test_retry_pending_completes_a_document_left_half_written(worker, db_path):
    """Un document dont l'archivage Drive avait echoue se termine tout seul."""
    worker.workbook.drive_fails = True
    worker.add_message(
        "m-pack", internal_date=worker.moment(),
        attachments={"Pack.zip": zip_of({f"pack/{ACHAT}.pdf": pdf_bytes(ACHAT)})},
    )
    worker.process_once()
    row = store.list_by_message(db_path, CHAT_ID, "m-pack")[0]
    assert row["state"] == store.PARTIAL
    written = len(worker.workbook.rows("05_FACTURES_ACHATS"))

    worker.workbook.drive_fails = False
    worker.retry_pending()

    after = store.get_document(db_path, row["doc_key"])
    assert after["state"] == store.COMPLETED
    assert after["drive_link"]
    assert len(worker.workbook.rows("05_FACTURES_ACHATS")) == written


# === migration des documents deja crees par l'ancienne version ===========

def test_a_document_stored_before_member_paths_existed_is_recovered(worker, db_path):
    """Les demandes deja presentes doivent revivre SANS renvoyer le ZIP.

    Les enregistrements crees par la version precedente n'ont ni chemin
    interne, ni copie locale, et portent un `attachment_id` Gmail devenu
    obsolete. La reprise doit les rattraper a partir de l'archive d'origine,
    en identifiant le bon membre par son empreinte.
    """
    send_pack(worker)
    pending = worker.process_once()[0].to_review[0]

    # On remet la fiche dans l'etat qu'avait produit l'ancienne version.
    store.update_document(
        db_path, pending.doc_key,
        member_path="", local_path="",
        parent_attachment_id="identifiant-perime",
        parent_filename="",
    )
    vault.discard(db_path, CHAT_ID, pending.doc_key)

    reply = worker.confirm(pending.doc_key[:24])

    assert "Document valide et enregistre" in reply
    restored = store.get_document(db_path, pending.doc_key)
    assert restored["state"] == store.COMPLETED
    assert restored["member_path"] == f"pack/{ANOMALIE}.pdf"   # fiche completee


def test_the_recovered_document_is_written_once_and_only_once(worker, db_path):
    send_pack(worker)
    pending = worker.process_once()[0].to_review[0]
    store.update_document(db_path, pending.doc_key, member_path="", local_path="")
    vault.discard(db_path, CHAT_ID, pending.doc_key)

    before = len(worker.workbook.rows("05_FACTURES_ACHATS"))
    worker.confirm(pending.doc_key[:24])
    once = len(worker.workbook.rows("05_FACTURES_ACHATS"))
    worker.confirm(pending.doc_key[:24])

    assert once == before + 1
    assert len(worker.workbook.rows("05_FACTURES_ACHATS")) == once


def test_the_vault_never_leaks_between_two_clients(db_path):
    """Cloisonnement : le coffre d'un client est inaccessible a un autre."""
    vault.save(db_path, 111, "cle-partagee", b"%PDF-client-a")
    assert vault.load(db_path, 222, "cle-partagee") is None
    assert vault.load(db_path, 111, "cle-partagee") == b"%PDF-client-a"


def test_the_vault_survives_a_restart(db_path):
    content = b"%PDF-durable"
    vault.save(db_path, CHAT_ID, "cle", content)
    assert vault.load(db_path, CHAT_ID, "cle", sha256_of(content)) == content


# === 12. le contenu archive est celui du document, pas celui du ZIP ======

def test_each_archived_pdf_carries_its_own_bytes_not_the_zip(worker):
    """Le defaut trouve en production : chaque PDF etait archive avec le
    contenu de l'archive parente. Le lien existait, la piece etait fausse."""
    send_pack(worker)
    worker.process_once()

    archive_entier = zip_of(PACK_MEMBERS)
    deposes = dict(worker.uploaded)
    assert deposes, "aucun contenu depose"
    assert archive_entier not in deposes.values()

    par_nom = {}
    for upload in worker.workbook.uploads:
        cible = upload["file_to_upload"]
        par_nom[cible["name"]] = deposes[cible["s3key"]]

    for chemin, contenu in PACK_MEMBERS.items():
        nom = chemin.split("/")[-1]
        assert par_nom[nom] == contenu, f"{nom} archive avec un contenu etranger"


def test_a_zip_member_is_never_archived_from_the_parent_url(worker):
    """Aucun repli sur l'URL Gmail pour un membre de ZIP : elle designe
    l'archive entiere. Mieux vaut ne rien archiver que du faux."""
    send_pack(worker)
    worker.uploaded.clear()

    def refuse(**kwargs):
        raise MailWorkerError("depot indisponible")

    worker.upload = refuse
    worker.process_once()

    assert not any(
        "source_url" in upload for upload in worker.workbook.uploads
    ), "un membre de ZIP a ete archive depuis l'URL du ZIP parent"


# === 13. apres validation, la piece et le journal suivent ================

def test_validation_moves_the_document_out_of_the_review_folder(worker):
    send_pack(worker)
    pending = worker.process_once()[0].to_review[0]
    assert pending.drive_link

    worker.confirm(pending.doc_key[:24])

    assert worker.workbook.moves, "la piece est restee dans 'A verifier'"
    move = worker.workbook.moves[-1]
    a_verifier = worker.workbook.folders.get(("", "A verifier"))
    assert move["remove_parents"] != ""
    assert move["add_parents"] != a_verifier


def test_validation_updates_the_pending_log_row_instead_of_adding_one(worker):
    send_pack(worker)
    pending = worker.process_once()[0].to_review[0]
    journal_avant = worker.workbook.rows("14_IMPORTS_LOG")
    ligne = next(
        index for index, row in enumerate(journal_avant, start=1)
        if row and row[3] == "A valider"
    )

    worker.confirm(pending.doc_key[:24])

    journal_apres = worker.workbook.rows("14_IMPORTS_LOG")
    assert len(journal_apres) == len(journal_avant), "une seconde ligne a ete creee"
    mise_a_jour = journal_apres[ligne - 1]
    assert mise_a_jour[3] == "Cree"
    assert "EN ATTENTE DE VALIDATION" not in mise_a_jour[5]
    assert "05_FACTURES_ACHATS" in mise_a_jour[5]
