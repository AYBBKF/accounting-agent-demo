"""L'architecture de quarantaine : `21_A_VERIFIER` remplace les boutons.

Ce module teste le contrat NOUVEAU, celui qui a remplace la validation par
bouton Telegram :

  - un document fiable est importe automatiquement ;
  - un document douteux est ECRIT dans `21_A_VERIFIER`, en rouge, avec le
    motif exact - jamais mis en attente derriere un bouton ;
  - ses montants sont du TEXTE, donc invisibles pour tout total, pour le
    Dashboard et pour la TVA ;
  - le relire n'ajoute jamais une seconde ligne.

Les trois premiers points sont des promesses faites au client. Le
quatrieme est ce qui les rend tenables dans la duree.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app import doc_store as store
from app.doc_policy import (
    ACTION_REVIEW,
    NotWritable,
    assert_writable,
)
from app.doc_extract import clean_party_name, extract_document
from app.review_sheet import (
    REVIEW_HEADERS,
    STATUS_TODO,
    TAB_REVIEW,
    ReviewEntry,
    build_detail,
    build_review_row,
    build_tooltip,
    find_row,
    format_amounts,
    summarize,
)

ANOMALIE = "09_FACTURE_ANOMALIE_FAC-TEST-2026-003_VALIDATION_REQUISE"

from test_mail_worker import FakeMailWorker, pdf_bytes, text_of
from workbook_fake import FakeWorkbook


# === 1. le module pur : ce qui est ecrit, et sous quelle forme ============

def entree(**kw) -> ReviewEntry:
    base = dict(
        doc_key="a" * 64,
        detected_at="2026-08-25T10:00:00+00:00",
        type_label="Facture d'achat",
        numero="FAC-V3-AMB-001",
        tiers="DATA NORTH V2 SARL",
        devise="MAD",
        montant_ht=Decimal("3000.00"),
        montant_tva=Decimal("600.00"),
        montant_ttc=Decimal("3900.00"),
        reasons=["HT + TVA ne correspond pas au TTC (ecart 300.00)"],
    )
    base.update(kw)
    return ReviewEntry(**base)


def test_every_written_cell_is_text_so_no_total_can_ever_catch_it():
    """La garantie centrale, et elle est structurelle.

    Si un montant douteux etait ecrit comme NOMBRE, il suffirait qu'une
    formule balaye la plage - aujourd'hui ou dans six mois - pour qu'il
    entre dans un total. En texte, c'est impossible.
    """
    row = build_review_row(entree())
    assert len(row) == len(REVIEW_HEADERS)
    assert all(isinstance(cell, str) for cell in row)


def test_the_amounts_say_out_loud_that_they_are_not_booked():
    texte = format_amounts(entree())
    assert "3000.00 MAD" in texte
    assert "600.00 MAD" in texte
    assert "3900.00 MAD" in texte
    assert "[lu, non comptabilise]" in texte


def test_an_unread_amount_is_never_shown_as_zero():
    """Un montant absent reste absent : zero serait un mensonge chiffre."""
    texte = format_amounts(entree(montant_ttc=None))
    assert "TTC non lu" in texte
    assert "TTC 0" not in texte


def test_the_detail_names_the_reason_before_anything_else():
    detail = build_detail(entree())
    assert detail.startswith("Ce document n'a PAS ete comptabilise")
    assert "HT + TVA ne correspond pas au TTC" in detail
    assert "n'entre dans les totaux, le Dashboard ou la TVA" in detail


def test_several_reasons_are_all_kept_in_the_detail():
    motifs = ["devise EUR sans taux de change", "ICE du tiers absent"]
    detail = build_detail(entree(reasons=motifs))
    for motif in motifs:
        assert motif in detail
    assert summarize(motifs).startswith(motifs[0])
    assert "+1 autre" in summarize(motifs)


def test_a_long_explanation_is_cut_cleanly_not_mid_word():
    """Une infobulle tronquee par Sheets au milieu d'un mot ne sert a rien."""
    motifs = [f"motif numero {i} explique longuement et sans abreviation" for i in range(20)]
    bulle = build_tooltip(entree(reasons=motifs))
    assert len(bulle) <= 320
    assert bulle.endswith("(detail complet en colonne I)")
    assert not bulle.rstrip(". ").endswith("abreviatio")


def test_a_document_is_found_by_its_key_not_by_its_position():
    """Les lignes bougent ; les cles, non."""
    colonne = ["autre1234567", "a" * 12, "autre7654321"]
    assert find_row(colonne, "a" * 64) == 3
    assert find_row(colonne, "inconnu" + "0" * 57) == 0
    assert find_row(colonne, "") == 0


def test_a_new_line_is_always_offered_to_the_accountant_as_todo():
    assert build_review_row(entree())[-1] == STATUS_TODO


# === 2. le verrou d'ecriture ==============================================

def doc_de(texte: str):
    return extract_document([texte])


def test_an_invoice_whose_totals_contradict_each_other_is_refused(tmp_path):
    doc = doc_de(text_of(ANOMALIE))
    with pytest.raises(NotWritable, match="HT \\+ TVA"):
        assert_writable(doc, "FRS-008")


def test_a_credit_note_without_a_party_id_is_refused():
    """Le defaut AV-V3-VTE-001 : un avoir impute a personne."""
    from app.doc_types import CLIENT_CREDIT_NOTE
    doc = doc_de(text_of(ANOMALIE))
    doc.classification.doc_type = CLIENT_CREDIT_NOTE
    doc.montant_ttc = None
    doc.montant_ht = None
    doc.montant_tva = None
    with pytest.raises(NotWritable, match="identifiant de tiers"):
        assert_writable(doc, "")


def test_a_foreign_currency_invoice_is_refused_even_at_the_last_moment():
    """Le defaut import/export EUR : jamais de conversion implicite."""
    doc = doc_de(text_of(ANOMALIE))
    doc.devise = "EUR"
    doc.montant_ttc = None
    with pytest.raises(NotWritable, match="EUR"):
        assert_writable(doc, "FRS-008")


def test_a_clean_invoice_in_dirhams_passes_the_lock():
    from test_document_pipeline import ACHAT as ACHAT_NAME
    texte = Path("tests/fixtures/pack") / f"{ACHAT_NAME}.txt"
    doc = doc_de(texte.read_text(encoding="utf-8"))
    assert_writable(doc, "FRS-006")     # ne doit pas lever


# === 3. les noms de tiers : plus de clients en double =====================

@pytest.mark.parametrize("brut, attendu", [
    ("Client : ATLAS CLINIQUE SARL", "ATLAS CLINIQUE SARL"),
    ("Client: NOVA DESIGN SARL", "NOVA DESIGN SARL"),
    ("Fournisseur - TECH OFFICE SARL", "TECH OFFICE SARL"),
    ("Bill To: EURO TECH SUPPLY GmbH", "EURO TECH SUPPLY GmbH"),
    ("ATLAS CLINIQUE SARL", "ATLAS CLINIQUE SARL"),
])
def test_a_role_label_never_becomes_part_of_a_company_name(brut, attendu):
    """Le defaut CLI-010 / CLI-011 : deux fiches pour une seule societe."""
    assert clean_party_name(brut) == attendu


def test_a_name_that_is_only_a_label_is_left_for_the_policy_to_reject():
    """On ne fabrique pas un nom vide en silence : la politique tranche."""
    from app.doc_policy import usable_party_name
    assert not usable_party_name(clean_party_name("Client"))
    assert clean_party_name("") is None


# === 4. bout en bout : le classeur reel ===================================

@pytest.fixture
def worker(tmp_path, monkeypatch):
    import app.doc_pipeline as module
    monkeypatch.setattr(
        module, "extract_from_pdf_bytes",
        lambda c, company="X BLASTE", ocr=True: extract_document([text_of(ANOMALIE)]),
    )
    return FakeMailWorker(FakeWorkbook(), str(tmp_path / "demo.db"))


def test_a_doubtful_document_is_written_in_the_review_tab_not_in_accounting(worker):
    worker.add_message(
        "m1", internal_date=worker.moment(),
        attachments={"anomalie.pdf": pdf_bytes(ANOMALIE)},
    )
    resultat = worker.process_once()[0]
    outcome = resultat.outcomes[0]

    assert outcome.action == ACTION_REVIEW
    assert outcome.tab == ""
    assert worker.workbook.writes_to("05_FACTURES_ACHATS") == []

    lignes = worker.workbook.rows(TAB_REVIEW)
    assert len(lignes) == 1
    ligne = lignes[0]
    assert ligne[0] == outcome.doc_key[:12]
    assert "HT + TVA" in ligne[7]
    assert "[lu, non comptabilise]" in ligne[6]
    assert ligne[11] == STATUS_TODO


def test_the_review_row_is_painted_red(worker):
    worker.add_message(
        "m1", internal_date=worker.moment(),
        attachments={"anomalie.pdf": pdf_bytes(ANOMALIE)},
    )
    worker.process_once()
    fonds = [
        args for slug, args in worker.workbook.calls
        if slug == "GOOGLESHEETS_FORMAT_CELL"
        and args.get("sheet_name") == TAB_REVIEW
        and args.get("background_color") == "#F4CCCC"
    ]
    assert fonds, "aucune ligne rouge dans 21_A_VERIFIER"


def test_reading_the_same_document_again_never_adds_a_second_row(worker):
    worker.add_message(
        "m1", internal_date=worker.moment(),
        attachments={"anomalie.pdf": pdf_bytes(ANOMALIE)},
    )
    worker.process_once()
    apres_un = len(worker.workbook.rows(TAB_REVIEW))

    worker.process_once()
    worker.process_once()
    worker.retry_pending()

    assert len(worker.workbook.rows(TAB_REVIEW)) == apres_un == 1


def test_the_review_tab_is_created_with_its_headers(worker):
    worker.add_message(
        "m1", internal_date=worker.moment(),
        attachments={"anomalie.pdf": pdf_bytes(ANOMALIE)},
    )
    worker.process_once()
    entete = worker.workbook.row(TAB_REVIEW, 1)
    assert entete == REVIEW_HEADERS


def test_no_telegram_validation_button_exists_anywhere():
    """Garde-fou : le bouton ne doit pas revenir par inadvertance."""
    import app.bot as bot
    import app.mail_worker as mw

    assert not hasattr(bot, "document_keyboard")
    assert not hasattr(mw, "MailWorker.confirm", )
    assert not hasattr(mw.MailWorker, "confirm")
    assert not hasattr(mw.MailWorker, "refuse")
    source = Path("app/bot.py").read_text(encoding="utf-8")
    assert "Valider et enregistrer" not in source
    assert "CALLBACK_CONFIRM_PREFIX" not in source
