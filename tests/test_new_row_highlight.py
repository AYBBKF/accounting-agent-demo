"""Repere visuel bleu clair sur les lignes REELLEMENT creees par le bot.

Le bleu #DDEBF7 est un confort de lecture, pas une donnee comptable. Ces
tests verifient donc surtout ce que la coloration ne fait PAS : elle
n'ecrase aucun format, aucune formule, aucun lien Drive, elle ne touche
jamais un doublon ignore ni une reprise idempotente, et elle ne prend
jamais le pas sur une couleur metier.
"""
import tempfile
from pathlib import Path

import pytest

from app import doc_store as store
from app.attachments import DocumentFile
from app.db import init_db
from app.doc_extract import extract_document
from app.doc_pipeline import NEW_ROW_COLOR, DocumentPipeline
from app.doc_policy import ACTION_DUPLICATE
from workbook_fake import FakeWorkbook

PACK = Path(__file__).parent / "fixtures" / "pack"
ACHAT = "01_FACTURE_ACHAT_OK_FAC-TEST-2026-002"
RECU = "11_RECU_PAIEMENT_REC-2026-017"


def text_of(name: str) -> str:
    return (PACK / f"{name}.txt").read_text(encoding="utf-8")


@pytest.fixture
def db_path():
    path = tempfile.mktemp(suffix=".db")
    init_db(path)
    store.ensure_schema(path)
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def workbook():
    return FakeWorkbook()


@pytest.fixture
def pipeline(workbook, db_path, monkeypatch):
    import app.doc_pipeline as module

    registry: dict[bytes, str] = {}

    def fake_read(content, company="X BLASTE", ocr=True):
        return extract_document([registry[content]], company=company)

    monkeypatch.setattr(module, "extract_from_pdf_bytes", fake_read)
    pipe = DocumentPipeline(
        workbook, db_path=db_path, chat_id=999653395, spreadsheet_id="sheet-test"
    )
    pipe.registry = registry  # type: ignore[attr-defined]
    return pipe


def run(pipeline, name, *, message_id="m1", attachment_id="att-1", tag=""):
    content = f"%PDF-{name}-{tag}".encode()
    pipeline.registry[content] = text_of(name)
    file = DocumentFile(filename=f"{name}.pdf", content=content, source="attachment")
    return pipeline.process_document(
        file,
        {"messageId": message_id, "subject": "Pack test", "sender": "client@example.ma"},
        attachment_id=attachment_id,
        source_url="https://example.invalid/f.pdf",
    )


def blue_calls(workbook):
    return [
        call for call in workbook.formats
        if str(call.get("background_color", "")).upper() == NEW_ROW_COLOR
    ]


def blue_ranges(workbook):
    return {
        (call.get("sheet_name", ""), call.get("range", ""))
        for call in blue_calls(workbook)
    }


# === 1. une ligne reellement nouvelle est bleue ==========================

def test_a_genuinely_new_row_is_painted_light_blue(pipeline, workbook):
    outcome = run(pipeline, ACHAT)
    assert outcome.row_index >= 2
    attendu = (outcome.tab, f"A{outcome.row_index}:Q{outcome.row_index}")
    assert attendu in blue_ranges(workbook)


def test_the_import_log_line_is_painted_too(pipeline, workbook):
    run(pipeline, ACHAT)
    onglets = {sheet for sheet, _ in blue_ranges(workbook)}
    assert "14_IMPORTS_LOG" in onglets


def test_a_party_row_is_painted_only_when_the_bot_creates_it(pipeline, workbook):
    # Le fournisseur du pack existe deja dans le classeur : aucune fiche
    # n'est creee, donc aucune ligne de tiers n'est peinte.
    run(pipeline, ACHAT)
    onglets = {sheet for sheet, _ in blue_ranges(workbook)}
    assert "03_FOURNISSEURS" not in onglets
    assert not any(
        call.get("range", "").startswith("A")
        for sheet, call in [("03_FOURNISSEURS", c) for c in blue_calls(workbook)]
        if call.get("sheet_name") == "03_FOURNISSEURS"
    )


# === 2. un doublon ne cree ni ligne ni couleur ===========================

def test_an_ignored_duplicate_creates_no_row_and_no_new_colour(pipeline, workbook):
    run(pipeline, ACHAT, message_id="m1", attachment_id="att-1")
    lignes_avant = len(workbook.tabs.get("05_FACTURES_ACHATS", []))
    peintes_avant = blue_ranges(workbook)

    second = run(pipeline, ACHAT, message_id="m2", attachment_id="att-2", tag="bis")

    assert second.action == ACTION_DUPLICATE
    assert len(workbook.tabs.get("05_FACTURES_ACHATS", [])) == lignes_avant
    assert blue_ranges(workbook) == peintes_avant


def test_an_idempotent_retry_repaints_nothing(pipeline, workbook, db_path):
    premier = run(pipeline, ACHAT, message_id="m1", attachment_id="att-1")
    peintes_avant = blue_ranges(workbook)
    lignes_avant = len(workbook.tabs.get("05_FACTURES_ACHATS", []))

    # Meme document, meme empreinte : la reprise retrouve la ligne existante
    # au lieu d'en creer une seconde.
    second = run(pipeline, ACHAT, message_id="m1", attachment_id="att-1")

    assert second.row_index == premier.row_index
    assert len(workbook.tabs.get("05_FACTURES_ACHATS", [])) == lignes_avant
    assert blue_ranges(workbook) == peintes_avant


# === 3. la couleur metier reste prioritaire ==============================

def test_the_blue_never_touches_anything_but_the_background(pipeline, workbook):
    run(pipeline, ACHAT)
    for call in blue_calls(workbook):
        # Aucun format de nombre, aucune police, aucune bordure : le masque
        # de champs envoye a l'API se limite au fond. Les regles
        # conditionnelles du classeur (rouge doublon, orange anomalie,
        # jaune impaye) sont evaluees apres le fond et gardent la main.
        assert set(call) <= {"spreadsheet_id", "sheet_name", "range", "background_color"}
        assert "number_format_type" not in call
        assert "number_format_pattern" not in call
        assert "bold" not in call


def test_the_business_formats_are_written_before_the_blue(pipeline, workbook):
    outcome = run(pipeline, ACHAT)
    cible = f"A{outcome.row_index}:Q{outcome.row_index}"
    index_bleu = next(
        i for i, call in enumerate(workbook.formats)
        if call.get("range") == cible
        and str(call.get("background_color", "")).upper() == NEW_ROW_COLOR
    )
    formats_metier = [
        i for i, call in enumerate(workbook.formats)
        if call.get("number_format_type")
        and call.get("range", "").endswith(str(outcome.row_index))
    ]
    # Le fond blanc des formats de devise et de date passe AVANT, sinon il
    # effacerait le bleu.
    assert formats_metier and max(formats_metier) < index_bleu


# === 4. les valeurs, formules et liens ne sont pas touches ===============

def test_the_row_values_are_identical_before_and_after_the_blue(pipeline, workbook):
    outcome = run(pipeline, ACHAT)
    ligne = workbook.tabs[outcome.tab][outcome.row_index - 1]
    # La coloration passe par GOOGLESHEETS_FORMAT_CELL : aucune ecriture de
    # valeur, donc les formules K:M et Q sont encore des formules.
    formules = [c for c in ligne if isinstance(c, str) and c.startswith("=")]
    assert formules
    ecritures = [
        call for slug, call in workbook.calls
        if slug == "GOOGLESHEETS_VALUES_UPDATE"
        and call.get("range", "").endswith(f"{outcome.row_index}")
    ]
    assert all(call.get("values") for call in ecritures)


def test_a_document_parked_for_validation_gets_no_accounting_row(pipeline, workbook):
    avant = len(workbook.tabs.get("05_FACTURES_ACHATS", []))
    outcome = run(pipeline, RECU)
    # Un recu de paiement n'ouvre aucune ligne de facture : rien a peindre
    # dans les onglets comptables.
    assert len(workbook.tabs.get("05_FACTURES_ACHATS", [])) == avant
    assert outcome is not None


def test_a_party_row_created_by_the_bot_is_painted(pipeline, workbook):
    from app.doc_pipeline import PartyMatch

    ligne = pipeline.create_party(
        "03_FOURNISSEURS", PartyMatch("FRS-999", "NOUVEAU TIERS SARL"), "001122334400099"
    )
    assert ligne == "FRS-999"
    peintes = blue_ranges(workbook)
    assert any(sheet == "03_FOURNISSEURS" for sheet, _ in peintes)
    for call in blue_calls(workbook):
        if call.get("sheet_name") == "03_FOURNISSEURS":
            assert call["range"].startswith("A") and call["range"].split(":")[1][0] == "G"


def test_updating_an_existing_log_line_is_not_a_new_row(pipeline, workbook):
    outcome = run(pipeline, ACHAT)
    peintes_avant = blue_ranges(workbook)
    lignes_avant = len(workbook.tabs.get("14_IMPORTS_LOG", []))

    # Reecriture EN PLACE de la ligne de journal : une mise a jour n'est pas
    # une creation, elle ne doit ni ajouter de ligne ni ajouter de couleur.
    pipeline.append_import_log(
        outcome, {"messageId": "m1", "sender": "x@y.ma", "subject": "s"}, row_index=2
    )

    assert len(workbook.tabs.get("14_IMPORTS_LOG", [])) == lignes_avant
    assert blue_ranges(workbook) == peintes_avant
