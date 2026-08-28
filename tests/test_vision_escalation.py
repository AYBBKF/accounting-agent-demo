"""Escalade de lecture Luna -> Terra -> Sol, et ses garde-fous.

Une PHOTO de facture met en defaut l'OCR : il fusionne les colonnes d'un
tableau ("NUMERO DATE ECHEANCE" sur une seule ligne), perd les decimales
("720.00" devient "72000") et manque le numero de piece. Le document
partait en quarantaine alors qu'il etait parfaitement lisible a l'oeil.

Sol relit les OCTETS DE L'IMAGE ORIGINALE - jamais le texte OCR degrade,
qui est precisement la cause du probleme. Rien n'est retenu sans franchir
six controles comptables.
"""
from datetime import date
from decimal import Decimal

import pytest

from app import doc_vision
from app.doc_vision import (
    VisionBudget,
    VisionResult,
    apply_vision,
    escalation_reasons,
    validate,
)
from app.doc_extract import extract_document

TODAY = date(2026, 8, 28)
RATES = (Decimal("0"), Decimal("7"), Decimal("10"), Decimal("20"))

# Reponse REELLE de Sol sur la photo IMG_20260818_102233.jpg du lot.
SOL_OK = {
    "type_document": "Facture fournisseur", "numero": "F2026-1106",
    "date": "2026-08-18", "tiers": "SAHARA LOGISTIQUE SARL",
    "ICE": "004556677000012", "HT": 3600.0, "taux_TVA": 20, "TVA": 720.0,
    "TTC": 4320.0, "devise": "MAD", "echeance": "2026-09-18",
    "confidence": 0.98,
    "evidence": ["NUMERO F2026-1106", "DATE 18/08/2026", "TotalHT 3 600.00 MAD",
                 "720.00 MAD", "TOTAL TTC 4 320.00 MAD"],
}
# Reponse REELLE de Sol sur la photo illisible IMG_20260820_213140.jpg.
SOL_VIDE = {
    "type_document": None, "numero": None, "date": None, "tiers": None,
    "ICE": None, "HT": None, "taux_TVA": None, "TVA": None, "TTC": None,
    "devise": None, "echeance": None, "confidence": 0.02, "evidence": [],
}

# Texte OCR reellement produit par Tesseract sur cette meme photo : colonnes
# fusionnees, decimales perdues.
OCR_DEGRADE = "\n".join([
    "FACTURE FOURNSSEUR",
    "FOURNSSEUR",
    "SAHARA LOGSTQUE SARL",
    "NUMERO DATE",
    "F 2026-1106 1808/2026",
    "TotalHT 3 600.00MAD",
    "TVA 2E+1 % 72000 MAD",
    "TOTAL TTC 432000MAD",
])


def result(data, level="sol"):
    return VisionResult(level=level, data=data,
                        confidence=float(data.get("confidence") or 0),
                        evidence=list(data.get("evidence") or []))


# --- 1. quand faut-il escalader ------------------------------------------

def test_a_degraded_photo_triggers_escalation():
    doc = extract_document([OCR_DEGRADE], company="X BLASTE", text_source="ocr")
    raisons = escalation_reasons(doc)
    assert raisons, "l'OCR degrade doit declencher l'escalade"
    assert any("numero" in r for r in raisons)


def test_a_clean_document_never_escalates():
    """Aucun appel modele quand la lecture deterministe suffit."""
    propre = "\n".join([
        "FACTURE FOURNISSEUR", "F2026-1101", "FOURNISSEUR", "ATLAS PRO SARL",
        "ICE fournisseur : 001122334455667", "CLIENT", "X BLASTE",
        "NUMERO", "DATE", "ECHEANCE", "F2026-1101", "15/08/2026", "15/09/2026",
        "Total HT", "5 000.00 MAD", "TVA 20 %", "1 000.00 MAD",
        "TOTAL TTC", "6 000.00 MAD",
    ])
    doc = extract_document([propre], company="X BLASTE")
    assert escalation_reasons(doc) == []


def test_a_bank_statement_never_escalates():
    """Un releve n'a ni numero ni HT/TVA/TTC : il suit son propre chemin."""
    releve = "\n".join(["RELEVE BANCAIRE", "REL-BP-2026-08",
                        "Periode : 01/08/2026 au 25/08/2026"])
    doc = extract_document([releve], company="X BLASTE")
    assert escalation_reasons(doc) == []


# --- 2. les six controles -------------------------------------------------

def test_a_coherent_reading_passes_every_check():
    assert validate(result(SOL_OK), today=TODAY, allowed_rates=RATES) == []


def test_the_decimals_are_only_accepted_with_visual_evidence():
    """"72000" ne devient "720,00" que si la lecture le PROUVE.

    Sans preuve citee, la valeur est refusee meme si elle est arithmetiquement
    coherente : c'est ce qui empeche d'inventer une decimale.
    """
    sans_preuve = dict(SOL_OK, evidence=[])
    echecs = validate(result(sans_preuve), today=TODAY, allowed_rates=RATES)
    assert any("preuve" in e for e in echecs)


def test_an_incoherent_total_is_refused():
    echecs = validate(result(dict(SOL_OK, TTC=9999.0)), today=TODAY, allowed_rates=RATES)
    assert any("TTC" in e for e in echecs)


def test_a_vat_amount_inconsistent_with_its_rate_is_refused():
    echecs = validate(result(dict(SOL_OK, TVA=100.0, TTC=3700.0)),
                      today=TODAY, allowed_rates=RATES)
    assert any("incoherente" in e for e in echecs)


def test_an_implausible_number_is_refused():
    echecs = validate(result(dict(SOL_OK, numero="AA")), today=TODAY, allowed_rates=RATES)
    assert any("numero" in e for e in echecs)


def test_a_future_date_is_refused():
    echecs = validate(result(dict(SOL_OK, date="2027-01-15")), today=TODAY, allowed_rates=RATES)
    assert any("futur" in e for e in echecs)


def test_an_implausible_ice_is_refused():
    echecs = validate(result(dict(SOL_OK, ICE="12")), today=TODAY, allowed_rates=RATES)
    assert any("ICE" in e for e in echecs)


def test_a_currency_outside_the_books_is_refused():
    echecs = validate(result(dict(SOL_OK, devise="EUR")), today=TODAY,
                      allowed_rates=RATES, allowed_currencies=("MAD",))
    assert any("EUR" in e for e in echecs)


def test_a_rate_outside_the_configuration_is_refused():
    echecs = validate(result(dict(SOL_OK, taux_TVA=21, TVA=756.0, TTC=4356.0)),
                      today=TODAY, allowed_rates=RATES)
    assert any("21" in e for e in echecs)


def test_an_empty_reading_is_refused():
    res = result(SOL_VIDE)
    assert res.is_empty
    assert validate(res, today=TODAY, allowed_rates=RATES)


# --- 3. report sur le document -------------------------------------------

def test_the_merged_document_carries_the_read_values():
    doc = extract_document([OCR_DEGRADE], company="X BLASTE", text_source="ocr")
    apply_vision(doc, result(SOL_OK))
    assert doc.numero == "F2026-1106"
    assert doc.date_document == date(2026, 8, 18)
    assert doc.date_echeance == date(2026, 9, 18)
    assert doc.montant_ht.value == Decimal("3600.0")
    assert doc.montant_tva.value == Decimal("720.0")     # et non 72000
    assert doc.montant_ttc.value == Decimal("4320.0")    # et non 432000
    assert doc.emetteur_ice == "004556677000012"
    assert doc.devise == "MAD"
    assert doc.text_source == "vision:sol"
    assert doc.anomalies == []


def test_the_merged_document_drops_the_degraded_anomalies():
    doc = extract_document([OCR_DEGRADE], company="X BLASTE", text_source="ocr")
    doc.anomalies.append("somme des lignes incoherente")
    apply_vision(doc, result(SOL_OK))
    assert doc.anomalies == []


# --- 4. budget d'appels ---------------------------------------------------

def test_the_vision_budget_is_strictly_capped():
    budget = VisionBudget(2)
    assert budget.take() and budget.take()
    assert budget.take() is False
    assert budget.used == 2 and budget.remaining == 0


def test_the_budget_is_reset_between_emails():
    budget = VisionBudget(1)
    assert budget.take() and budget.take() is False
    budget.reset()
    assert budget.take()


def test_a_zero_budget_forbids_every_call():
    assert VisionBudget(0).take() is False


# --- 5. orchestration : Luna -> Terra -> Sol ------------------------------

class FakeVision:
    """Faux extracteur : journalise les niveaux appeles et ce qu'il recoit."""

    available = True

    def __init__(self, terra=None, sol=None):
        self.calls: list[str] = []
        self.image_bytes: bytes | None = None
        self.text_seen: str | None = None
        self._terra, self._sol = terra, sol

    def read_text(self, texte):
        self.calls.append("terra")
        self.text_seen = texte
        return result(self._terra, level="terra") if self._terra else None

    def read_image(self, data, mimetype):
        self.calls.append("sol")
        self.image_bytes = data
        return result(self._sol, level="sol") if self._sol else None


class FakeFile:
    def __init__(self, content, filename="photo.jpg"):
        self.content = content
        self.filename = filename


@pytest.fixture
def db_path():
    import tempfile
    from pathlib import Path

    from app.db import init_db
    from app import doc_store as store

    path = tempfile.mktemp(suffix=".db")
    init_db(path)
    store.ensure_schema(path)
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def workbook():
    from tests.workbook_fake import FakeWorkbook

    return FakeWorkbook()


def build_pipeline(vision, budget, db_path, workbook):
    from app.doc_pipeline import DocumentPipeline

    return DocumentPipeline(
        workbook, db_path=db_path, chat_id=999653395, spreadsheet_id="s",
        allowed_vat_rates=RATES, today=lambda: TODAY,
        vision=vision, vision_budget=budget,
    )


@pytest.fixture
def jpeg_bytes():
    """Vrais octets JPEG : c'est eux que Sol doit recevoir."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), (255, 255, 255)).save(buffer, format="JPEG")
    return buffer.getvalue()


def degraded_doc():
    return extract_document([OCR_DEGRADE], company="X BLASTE", text_source="ocr")


def test_sol_receives_the_original_image_not_the_bad_ocr_text(jpeg_bytes, db_path, workbook):
    vision = FakeVision(terra=None, sol=SOL_OK)
    pipe = build_pipeline(vision, VisionBudget(3), db_path, workbook)
    doc = degraded_doc()
    pipe.escalate_reading(doc, FakeFile(jpeg_bytes))
    assert vision.image_bytes == jpeg_bytes
    assert doc.numero == "F2026-1106"


def test_the_ladder_stops_as_soon_as_a_level_succeeds(jpeg_bytes, db_path, workbook):
    """Terra reussit : Sol, plus couteux, n'est jamais appele."""
    vision = FakeVision(terra=SOL_OK, sol=SOL_OK)
    pipe = build_pipeline(vision, VisionBudget(3), db_path, workbook)
    pipe.escalate_reading(degraded_doc(), FakeFile(jpeg_bytes))
    assert vision.calls == ["terra"]


def test_the_ladder_climbs_to_sol_when_terra_fails(jpeg_bytes, db_path, workbook):
    vision = FakeVision(terra=dict(SOL_OK, TTC=1.0), sol=SOL_OK)
    pipe = build_pipeline(vision, VisionBudget(3), db_path, workbook)
    doc = degraded_doc()
    pipe.escalate_reading(doc, FakeFile(jpeg_bytes))
    assert vision.calls == ["terra", "sol"]
    assert doc.montant_ttc.value == Decimal("4320.0")


def test_a_document_all_three_levels_fail_stays_unreadable(jpeg_bytes, db_path, workbook):
    """Aucun niveau ne rend de valeur valide : rien n'est ecrit dans le document."""
    vision = FakeVision(terra=None, sol=SOL_VIDE)
    pipe = build_pipeline(vision, VisionBudget(3), db_path, workbook)
    doc = degraded_doc()
    pipe.escalate_reading(doc, FakeFile(jpeg_bytes))
    assert doc.numero is None
    assert doc.text_source == "ocr"          # aucune lecture escaladee retenue
    from app.doc_policy import ACTION_AUTO, DecisionContext, decide

    # Rien n'a ete lu : le document ne peut en aucun cas etre comptabilise.
    assert decide(doc, DecisionContext(today=TODAY)).action != ACTION_AUTO


def test_the_budget_stops_sol_and_leaves_a_traceable_reason(jpeg_bytes, db_path, workbook):
    """Le plafond porte sur SOL, le niveau qui lit une image entiere."""
    vision = FakeVision(terra=None, sol=SOL_OK)
    pipe = build_pipeline(vision, VisionBudget(0), db_path, workbook)
    doc = degraded_doc()
    pipe.escalate_reading(doc, FakeFile(jpeg_bytes))
    assert "sol" not in vision.calls
    assert doc.numero is None
    assert any("budget" in a for a in doc.anomalies)


def test_the_budget_only_counts_sol_calls(jpeg_bytes, db_path, workbook):
    """Terra, qui ne relit que du texte, ne consomme pas le budget vision."""
    budget = VisionBudget(1)
    vision = FakeVision(terra=SOL_OK, sol=SOL_OK)
    pipe = build_pipeline(vision, budget, db_path, workbook)
    pipe.escalate_reading(degraded_doc(), FakeFile(jpeg_bytes))
    assert vision.calls == ["terra"]
    assert budget.used == 0


def test_without_a_vision_client_the_behaviour_is_unchanged(jpeg_bytes, db_path, workbook):
    pipe = build_pipeline(None, VisionBudget(3), db_path, workbook)
    doc = degraded_doc()
    pipe.escalate_reading(doc, FakeFile(jpeg_bytes))
    assert doc.text_source == "ocr" and doc.numero is None


# --- une piece deja en quarantaine ne se relit pas indefiniment ---------


INCOHERENT = "\n".join([
    "FACTURE FOURNISSEUR",
    "Fournisseur : OMEGA SERVICES SARL",
    "ICE fournisseur : 006677889900112",
    "Numero de facture : F2026-1103",
    "Date de facture : 20/08/2026",
    "Client : X BLASTE",
    "ICE client : 001987654000021",
    "Total HT : 5 000,00 MAD",
    "TVA 20% : 1 000,00 MAD",
    "Total TTC : 6 450,00 MAD",
])


def incoherent_doc():
    """Facture lisible mais dont HT + TVA ne fait pas le TTC : quarantaine."""
    return extract_document([INCOHERENT], company="X BLASTE", text_source="pdf")


def test_une_piece_deja_en_quarantaine_ne_rappelle_plus_la_vision(
    db_path, workbook, monkeypatch
):
    """Le cycle Gmail repasse toutes les cinq minutes sur le meme email.

    Une piece garee dans "21_A_VERIFIER" y est reexaminee - c'est voulu,
    un humain peut l'avoir corrigee - mais ses OCTETS n'ont pas change :
    la cle d'idempotence inclut leur empreinte. Relire l'image avec les
    memes modeles rendrait exactement le meme resultat, et facturerait un
    appel de vision toutes les cinq minutes, indefiniment.
    """
    from app import doc_pipeline
    from app.attachments import DocumentFile

    # Facture aux totaux incoherents : elle finira en quarantaine. On
    # court-circuite la lecture du PDF pour que le test ne depende
    # d'aucune bibliotheque de rendu.
    monkeypatch.setattr(
        doc_pipeline, "extract_from_pdf_bytes",
        lambda *a, **k: incoherent_doc(),
    )

    vision = FakeVision(terra=None, sol=SOL_VIDE)
    pipe = build_pipeline(vision, VisionBudget(9), db_path, workbook)
    fichier = DocumentFile(
        filename="FACT_OMEGA_F2026-1103.pdf",
        content=b"%PDF-1.4 contenu simule",
        source="attachment",
    )
    message = {"messageId": "msg-quarantaine", "subject": "lot", "sender": "a@b.c"}

    premier = pipe.process_document(fichier, message, attachment_id="att-1")
    appels_premier_cycle = list(vision.calls)
    assert appels_premier_cycle, "le premier passage doit bien tenter l'escalade"

    pipe.process_document(fichier, message, attachment_id="att-1")
    assert vision.calls == appels_premier_cycle, (
        "aucun appel de vision supplementaire ne doit etre facture "
        "pour une piece deja garee en quarantaine"
    )
