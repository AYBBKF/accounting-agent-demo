"""Un mouvement bancaire ne disparait jamais en silence.

Le releve de production porte deux operations rigoureusement identiques -
meme date, meme libelle "VIR OMEGA FOURNITURES - FAC-ACH-2026-504", meme
reference. Le code avait deux comportements possibles face a cela, et les
deux etaient mauvais :

  - si l'empreinte etait deja connue, la seconde ligne etait RETIREE de la
    liste, sans journal, sans anomalie, sans trace ;
  - sinon, les deux etaient ecrites sans que rien ne signale la
    coincidence.

Le bot n'a pas a decider si un double virement est une erreur : deux
paiements identiques le meme jour existent. Il doit seulement refuser de
faire disparaitre un mouvement, et le dire.
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app import doc_store as store
from app.db import init_db
from app.doc_extract import BankLine, ExtractedDocument
from app.doc_pipeline import DocumentPipeline
from app.doc_routing import bank_line_fingerprint
from app.doc_types import BANK_STATEMENT, Classification
from app.review_sheet import TAB_REVIEW
from workbook_fake import FakeWorkbook

COMPTE = "Banque Principale DEMO"
CHAT = 999653395


def operation(libelle: str, montant: str, jour: date = date(2026, 8, 20)) -> BankLine:
    return BankLine(
        date_operation=jour, libelle=libelle, reference="REF-504",
        debit=Decimal(montant), credit=None, solde=None, devise="MAD", page=1,
    )


def releve(*lignes: BankLine) -> ExtractedDocument:
    doc = ExtractedDocument(
        classification=Classification(
            doc_type=BANK_STATEMENT, confidence=1.0, matched=[], reasons=[],
        )
    )
    doc.destinataire = COMPTE
    doc.bank_lines = list(lignes)
    return doc


@pytest.fixture
def pipeline(tmp_path):
    chemin = str(tmp_path / "demo.db")
    init_db(chemin)
    store.ensure_schema(chemin)
    workbook = FakeWorkbook()
    tuyau = DocumentPipeline(
        workbook, db_path=chemin, chat_id=CHAT, spreadsheet_id="sheet-test",
    )
    tuyau.workbook = workbook
    return tuyau


VIREMENT = "VIR OMEGA FOURNITURES - FAC-ACH-2026-504"


# === 1. le mouvement repete est CONSERVE et signale ======================

def test_two_identical_movements_are_both_written(pipeline):
    """Aucune suppression : les deux lignes existent dans le classeur."""
    ecrites, _ = pipeline.write_bank_statement(
        releve(operation(VIREMENT, "1200.00"), operation(VIREMENT, "1200.00")),
        doc_key="releve-aout",
    )
    assert ecrites == 2


def test_the_repeat_raises_an_anomaly_in_the_review_tab(pipeline):
    pipeline.write_bank_statement(
        releve(operation(VIREMENT, "1200.00"), operation(VIREMENT, "1200.00")),
        doc_key="releve-aout",
    )
    lignes = pipeline.workbook.rows(TAB_REVIEW)
    assert len(lignes) == 1
    detail = " ".join(str(c) for c in lignes[0])
    assert "Double paiement possible" in detail
    assert "conserv" in detail            # "les deux mouvements sont conserves"


def test_the_anomaly_amount_is_text_and_never_enters_a_total(pipeline):
    """Comme toute ligne de quarantaine : du texte, jamais un nombre."""
    pipeline.write_bank_statement(
        releve(operation(VIREMENT, "1200.00"), operation(VIREMENT, "1200.00")),
        doc_key="releve-aout",
    )
    ligne = pipeline.workbook.rows(TAB_REVIEW)[0]
    assert all(isinstance(cellule, str) for cellule in ligne)


# === 2. le meme releve reecrit ne duplique RIEN ==========================

def test_rewriting_the_same_statement_adds_nothing_and_raises_nothing(pipeline):
    """Distinction essentielle : reecrire n'est pas repeter.

    Sans elle, toute reprise apres incident aurait produit une fausse
    alerte de double paiement sur chaque operation du releve.
    """
    doc = releve(operation("VIR FOURNISSEUR A", "500.00"),
                 operation("VIR FOURNISSEUR B", "700.00"))
    premier, _ = pipeline.write_bank_statement(doc, doc_key="releve-aout")
    second, _ = pipeline.write_bank_statement(doc, doc_key="releve-aout")

    assert premier == 2
    assert second == 0
    assert pipeline.workbook.rows(TAB_REVIEW) == []


def test_a_different_statement_repeating_a_movement_is_signalled(pipeline):
    """Deux releves qui se chevauchent : le mouvement commun est signale.

    C'est le cas le plus delicat, et l'ancien code le traitait en
    supprimant : un chevauchement de releves EST une repetition du point
    de vue du classeur, et seul un humain peut trancher.
    """
    pipeline.write_bank_statement(
        releve(operation(VIREMENT, "1200.00")), doc_key="releve-juillet",
    )
    pipeline.write_bank_statement(
        releve(operation(VIREMENT, "1200.00")), doc_key="releve-aout",
    )
    lignes = pipeline.workbook.rows(TAB_REVIEW)
    assert len(lignes) == 1
    assert "releve-juill" in " ".join(str(c) for c in lignes[0])


def test_the_same_double_payment_never_raises_two_alerts(pipeline):
    """Relire le releve ne doit pas empiler les alertes."""
    doc = releve(operation(VIREMENT, "1200.00"), operation(VIREMENT, "1200.00"))
    pipeline.write_bank_statement(doc, doc_key="releve-aout")
    pipeline.write_bank_statement(doc, doc_key="releve-aout")
    assert len(pipeline.workbook.rows(TAB_REVIEW)) == 1


# === 3. l'empreinte reste la garantie de fond ============================

def test_each_occurrence_is_reserved_separately(pipeline):
    """L'empreinte porte le RANG de l'occurrence, et c'est ce qui fait
    tenir l'idempotence : deux mouvements identiques reservent deux cles
    distinctes, donc une relecture ne trouve plus rien de libre."""
    base = bank_line_fingerprint(COMPTE, operation(VIREMENT, "1200.00"))
    pipeline.write_bank_statement(
        releve(operation(VIREMENT, "1200.00"), operation(VIREMENT, "1200.00")),
        doc_key="releve-aout",
    )
    assert store.bank_line_owner(pipeline._db, f"{base}#1") == "releve-aout"
    assert store.bank_line_owner(pipeline._db, f"{base}#2") == "releve-aout"
    assert store.bank_line_owner(pipeline._db, f"{base}#3") == ""


# === 4. le rejeu, defaut remonte par la revue independante ===============

def test_replaying_the_same_statement_writes_nothing_more(pipeline):
    """LE defaut : 2 lignes, puis 3, puis 4 a chaque relecture.

    La premiere occurrence reservait l'empreinte ; la seconde, identique,
    n'en avait aucune a elle. A chaque relecture elle repassait donc pour
    une operation neuve. Les occurrences sont desormais numerotees.
    """
    doc = releve(operation(VIREMENT, "1200.00"), operation(VIREMENT, "1200.00"))

    premier, _ = pipeline.write_bank_statement(doc, doc_key="releve-aout")
    assert premier == 2

    second, _ = pipeline.write_bank_statement(doc, doc_key="releve-aout")
    assert second == 0

    troisieme, _ = pipeline.write_bank_statement(doc, doc_key="releve-aout")
    assert troisieme == 0

    lignes = pipeline.workbook.rows("06_RELEVE_BANCAIRE")
    assert len(lignes) == 2
    assert len(pipeline.workbook.rows(TAB_REVIEW)) == 1


def test_three_identical_movements_are_all_kept_and_stay_three(pipeline):
    """Trois virements identiques restent trois, meme apres relecture."""
    doc = releve(*[operation(VIREMENT, "1200.00") for _ in range(3)])

    assert pipeline.write_bank_statement(doc, doc_key="releve-aout")[0] == 3
    assert pipeline.write_bank_statement(doc, doc_key="releve-aout")[0] == 0
    assert len(pipeline.workbook.rows("06_RELEVE_BANCAIRE")) == 3


def test_a_statement_growing_by_one_movement_writes_only_that_one(pipeline):
    """Un releve complete en cours de mois n'ecrit que la ligne nouvelle."""
    pipeline.write_bank_statement(
        releve(operation(VIREMENT, "1200.00")), doc_key="releve-aout",
    )
    ajoutees, _ = pipeline.write_bank_statement(
        releve(operation(VIREMENT, "1200.00"), operation("VIR AUTRE", "300.00")),
        doc_key="releve-aout",
    )
    assert ajoutees == 1
    assert len(pipeline.workbook.rows("06_RELEVE_BANCAIRE")) == 2
