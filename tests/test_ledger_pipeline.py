"""Le journal comptable dans le pipeline reel : de la piece a l'ecriture.

Ces tests rejouent le pipeline complet (faux classeur, vrais PDF du pack
client) et verifient ce que la comptabilite exige : chaque document
comptabilise pose son ecriture equilibree dans 12_JOURNAL_COMPTABLE, une
quarantaine n'en pose AUCUNE, un retraitement n'en double aucune, et le
recapitulatif TVA suit les ecritures validees - par entreprise.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from app import doc_store as store
from app import ledger
from app.attachments import DocumentFile
from app import tenancy
from app.db import init_db
from app.doc_extract import extract_document
from app.doc_pipeline import DocumentPipeline
from workbook_fake import FakeWorkbook

PACK = Path(__file__).parent / "fixtures" / "pack"
ACHAT = "01_FACTURE_ACHAT_OK_FAC-TEST-2026-002"
VENTE = "02_FACTURE_VENTE_OK_FAC-VTE-TEST-2026-012"
ANOMALIE = "09_FACTURE_ANOMALIE_FAC-TEST-2026-003_VALIDATION_REQUISE"
AVOIR = "08_AVOIR_AV-2026-003_VALIDATION_REQUISE"

D = Decimal


def text_of(name: str) -> str:
    return (PACK / f"{name}.txt").read_text(encoding="utf-8")


@pytest.fixture
def db_path():
    path = tempfile.mktemp(suffix=".db")
    init_db(path)
    store.ensure_schema(path)
    tenancy.migrate_to_multi_tenant(path)
    yield path
    Path(path).unlink(missing_ok=True)


def _pipeline(db_path, monkeypatch, workbook, company_id="xblaste"):
    import app.doc_pipeline as module

    registry: dict[bytes, str] = getattr(workbook, "_registry", None) or {}
    workbook._registry = registry

    def fake_read(content, company="X BLASTE", ocr=True):
        return extract_document([registry[content]], company=company)

    monkeypatch.setattr(module, "extract_from_pdf_bytes", fake_read)
    pipe = DocumentPipeline(
        workbook, db_path=db_path, chat_id=999653395,
        spreadsheet_id=f"sheet-{company_id}", company_id=company_id,
    )
    pipe.registry = registry  # type: ignore[attr-defined]
    return pipe


def _run(pipe, name, *, message_id="m1", tag=""):
    content = f"%PDF-{name}-{tag}".encode()
    pipe.registry[content] = text_of(name)
    file = DocumentFile(filename=f"{name}.pdf", content=content, source="attachment")
    return pipe.process_document(
        file,
        {"messageId": message_id, "subject": "Pack", "sender": "c@example.ma"},
        attachment_id="att-1",
        source_url="https://example.invalid/f.pdf",
    )


def _journal_rows(workbook):
    lignes = []
    for w in workbook.writes_to("12_JOURNAL_COMPTABLE"):
        lignes.extend(w["values"])
    return lignes


def test_une_facture_d_achat_pose_son_ecriture_equilibree(db_path, monkeypatch):
    wb = FakeWorkbook()
    pipe = _pipeline(db_path, monkeypatch, wb)
    outcome = _run(pipe, ACHAT)
    assert not outcome.error

    entrees = ledger.entries_for(db_path, "xblaste")
    assert entrees, "la comptabilisation doit poser une ecriture"
    piece = entrees[0]["piece"]
    debit = sum(D(l["debit"]) for l in entrees)
    credit = sum(D(l["credit"]) for l in entrees)
    assert debit == credit != D("0")
    assert all(l["statut"] == "VALIDEE" for l in entrees)
    assert entrees[0]["gmail_message_id"] == "m1"
    assert entrees[0]["doc_sha256"], "l'ecriture garde le lien vers sa piece"

    lignes = _journal_rows(wb)
    assert len(lignes) == len(entrees)
    assert lignes[0][2] == piece
    comptes = {l[3] for l in lignes}
    assert comptes == {"6111", "3455", "4411"}


def test_une_facture_de_vente_alimente_le_recap_tva(db_path, monkeypatch):
    wb = FakeWorkbook()
    pipe = _pipeline(db_path, monkeypatch, wb)
    _run(pipe, VENTE)

    recap = ledger.tva_recap(db_path, "xblaste")
    assert recap and D(recap[0]["tva_collectee"]) > 0
    ecrits = wb.writes_to("BOT_TVA_RECAP")
    assert ecrits, "le recapitulatif doit etre projete dans le classeur"


def test_une_quarantaine_ne_pose_aucune_ecriture(db_path, monkeypatch):
    """La facture aux totaux incoherents part en 21_A_VERIFIER : le
    journal ne doit PAS en porter la trace."""
    wb = FakeWorkbook()
    pipe = _pipeline(db_path, monkeypatch, wb)
    outcome = _run(pipe, ANOMALIE)
    assert outcome.action != "auto"

    assert ledger.entries_for(db_path, "xblaste") == []
    assert _journal_rows(wb) == []


def test_le_retraitement_ne_double_aucune_ecriture(db_path, monkeypatch):
    wb = FakeWorkbook()
    pipe = _pipeline(db_path, monkeypatch, wb)
    _run(pipe, ACHAT)
    apres_premier = len(ledger.entries_for(db_path, "xblaste"))
    lignes_sheet_1 = len(_journal_rows(wb))

    for _ in range(3):
        _run(pipe, ACHAT)

    assert len(ledger.entries_for(db_path, "xblaste")) == apres_premier
    assert len(_journal_rows(wb)) == lignes_sheet_1


def test_deux_entreprises_ont_chacune_leur_journal(db_path, monkeypatch):
    """La meme facture envoyee a deux societes : une ecriture CHACUNE,
    dans SON classeur, et aucune ligne croisee."""
    wb_x, wb_s = FakeWorkbook(), FakeWorkbook()
    pipe_x = _pipeline(db_path, monkeypatch, wb_x, "xblaste")
    _run(pipe_x, ACHAT)
    pipe_s = _pipeline(db_path, monkeypatch, wb_s, "v2-smoke")
    _run(pipe_s, ACHAT, message_id="m2")

    ex = ledger.entries_for(db_path, "xblaste")
    es = ledger.entries_for(db_path, "v2-smoke")
    assert ex and es
    assert {l["piece"] for l in ex} == {l["piece"] for l in es}, (
        "meme piece, une fois dans chaque societe"
    )
    assert _journal_rows(wb_x) and _journal_rows(wb_s)
    assert ledger.tva_recap(db_path, "xblaste") != []
    # Le recap de chaque societe ne compte que SES ecritures.
    for r in ledger.balance_report(db_path, "v2-smoke").values():
        assert r["ecart"] == D("0")


def test_sans_entreprise_le_pipeline_reste_muet_au_journal(db_path, monkeypatch):
    """Mode mono-entreprise historique : aucun changement de comportement."""
    wb = FakeWorkbook()
    pipe = _pipeline(db_path, monkeypatch, wb, company_id="")
    _run(pipe, ACHAT)
    assert _journal_rows(wb) == []


def test_chaque_ecriture_pointe_vers_son_archive(db_path, monkeypatch):
    """Le lien ecriture -> piece est la colonne vertebrale de l'audit."""
    wb = FakeWorkbook()
    pipe = _pipeline(db_path, monkeypatch, wb)
    outcome = _run(pipe, ACHAT)
    entrees = ledger.entries_for(db_path, "xblaste")
    assert outcome.drive_link
    assert entrees[0]["drive_file_id"], (
        "l'ecriture doit porter l'identifiant Drive de sa piece"
    )
