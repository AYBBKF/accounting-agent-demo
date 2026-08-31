"""Relecture escaladee : type de document et ICE portes par la vision.

Constat du rejeu multi-entreprises reel : Sol lisait parfaitement les
photos (numero, montants, confiance 98-99 %) mais la piece restait en
quarantaine, soit "aucun marqueur de type reconnu" (le type vu par le
modele n'etait jamais applique), soit "facture fournisseur sans ICE
exploitable" (l'ICE, invisible dans le texte OCR, n'etait jamais demande
au niveau image). Ces tests forcent les deux chemins - sans toucher au
moindre seuil de decision comptable.
"""
from __future__ import annotations

import io
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app import doc_store as store
from app import doc_vision
from app.attachments import DocumentFile
from app.db import init_db
from app.doc_extract import extract_document
from app.doc_pipeline import DocumentPipeline
from app.doc_policy import ACTION_AUTO
from app.doc_vision import VisionResult, apply_vision, escalation_reasons, type_from_vision
from workbook_fake import FakeWorkbook

TODAY = date(2026, 8, 30)

# Texte OCR reel d'une photo : type et montants lus, ICE absent du texte.
OCR_SANS_ICE = "\n".join([
    "FACTURE FOURNISSEUR",
    "FA -DEM -2026-009",
    "FOURNISSEUR",
    "TRANSPORT RAPIDE DEMO SARL",
    "Numero de facture : FA-DEM-2026-009",
    "Date de facture : 2026-08-24",
    "Total HT",
    "3 600.00 MAD",
    "TVA 20 %",
    "720.00 MAD",
    "Total TTC",
    "4 320.00 MAD",
])

# Texte OCR si degrade qu'aucun marqueur de type n'est reconnu.
OCR_SANS_TYPE = "\n".join([
    "FA-DEM-2026-010",
    "SCAN SOLUTDNS DEMO SARL",
    "3 220.0 MAD",
    "644.0 MAD",
    "3 864.0 MAD",
])

VISION_ACHAT = {
    "type_document": "facture_achat", "numero": "FA-DEM-2026-009",
    "date": "2026-08-24", "tiers": "TRANSPORT RAPIDE DEMO SARL",
    "ICE": "009999000004401", "HT": 3600.0, "taux_TVA": 20, "TVA": 720.0,
    "TTC": 4320.0, "devise": "MAD", "echeance": None, "confidence": 0.98,
    "evidence": ["FACTURE FOURNISSEUR", "FA-DEM-2026-009", "ICE : 009999000004401",
                 "Total TTC 4 320.00 MAD"],
}
VISION_ACHAT_SANS_ICE = dict(VISION_ACHAT, ICE=None)


def _resultat(data, level="sol"):
    return VisionResult(level=level, data=data,
                        confidence=float(data.get("confidence") or 0),
                        evidence=list(data.get("evidence") or []))


def _png() -> bytes:
    from PIL import Image

    img = Image.new("RGB", (200, 100), "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --- type de document fourni par la vision --------------------------------

def test_le_type_vu_par_la_vision_est_reconnu_et_normalise():
    assert type_from_vision({"type_document": "facture_achat"}) == "facture_achat"
    assert type_from_vision({"type_document": "Facture fournisseur"}) == "facture_achat"
    assert type_from_vision({"type_document": "Recu de paiement"}) == "recu_paiement"
    assert type_from_vision({"type_document": "attestation"}) is None
    assert type_from_vision({"type_document": None}) is None


def test_un_document_sans_marqueur_recoit_le_type_vu_par_la_vision():
    doc = extract_document([OCR_SANS_TYPE], company="DEMO", text_source="ocr")
    assert doc.doc_type == "inconnu"
    apply_vision(doc, _resultat(dict(VISION_ACHAT, numero="FA-DEM-2026-010")))
    assert doc.doc_type == "facture_achat"
    assert doc.classification.matched == "vision:sol"
    # Jamais plus confiant que la relecture elle-meme, plafonne a 95 %.
    assert doc.classification.confidence <= 0.95


def test_un_type_deterministe_sur_ne_se_fait_jamais_ecraser():
    doc = extract_document([OCR_SANS_ICE], company="DEMO", text_source="ocr")
    assert doc.doc_type == "facture_achat"
    assert doc.classification.confidence >= 0.90
    apply_vision(doc, _resultat(dict(VISION_ACHAT, type_document="facture_vente")))
    assert doc.doc_type == "facture_achat"


def test_un_type_hors_liste_fermee_est_ignore():
    doc = extract_document([OCR_SANS_TYPE], company="DEMO", text_source="ocr")
    apply_vision(doc, _resultat(dict(VISION_ACHAT, type_document="bon cadeau")))
    assert doc.doc_type == "inconnu"


# --- ICE fournisseur absent d'une lecture degradee ------------------------

def test_un_achat_ocr_sans_ice_declenche_l_escalade():
    doc = extract_document([OCR_SANS_ICE], company="DEMO", text_source="ocr")
    assert doc.emetteur_ice is None
    raisons = escalation_reasons(doc)
    assert any("ICE du fournisseur" in r for r in raisons)


def test_un_achat_natif_sans_ice_n_escalade_pas_pour_l_ice():
    """Un PDF natif est fidele : l'ICE est reellement absent de la piece,
    aucune relecture ne l'inventera (cas FA-006 de la matrice)."""
    doc = extract_document([OCR_SANS_ICE], company="DEMO", text_source="native")
    raisons = escalation_reasons(doc)
    assert not any("ICE du fournisseur" in r for r in raisons)


class _FakeVision:
    """Terra lit le texte (sans ICE), Sol lit l'image (avec ICE)."""

    available = True

    def __init__(self):
        self.appels: list[str] = []

    def read_text(self, texte):
        self.appels.append("terra")
        return _resultat(VISION_ACHAT_SANS_ICE, level="terra")

    def read_image(self, data, mimetype):
        self.appels.append("sol")
        return _resultat(VISION_ACHAT, level="sol")

    def model_for(self, level):
        return f"modele-{level}"


@pytest.fixture
def pipeline(monkeypatch):
    import app.doc_pipeline as module

    db_path = tempfile.mktemp(suffix=".db")
    init_db(db_path)
    store.ensure_schema(db_path)
    registry: dict[bytes, str] = {}

    def fake_image_read(content, company="X BLASTE", ocr=True):
        return extract_document([registry[content]], company=company,
                                text_source="ocr")

    monkeypatch.setattr(module, "extract_from_image_bytes", fake_image_read)
    vision = _FakeVision()
    pipe = DocumentPipeline(
        FakeWorkbook(), db_path=db_path, chat_id=999653395,
        spreadsheet_id="sheet-test", company="DEMO",
        allowed_vat_rates=(Decimal("20"),),
        vision=vision, vision_budget=doc_vision.VisionBudget(6),
        today=lambda: TODAY,
    )
    pipe.registry = registry  # type: ignore[attr-defined]
    pipe.fake_vision = vision  # type: ignore[attr-defined]
    yield pipe
    Path(db_path).unlink(missing_ok=True)


def test_terra_sans_ice_continue_vers_sol_et_la_photo_est_comptabilisee(pipeline):
    """Terra rend une lecture valide mais SANS ICE (le texte ne le porte
    pas) : l'escalade continue vers Sol, qui voit l'image et fournit l'ICE.
    La piece est alors comptabilisee normalement - avant le correctif elle
    restait 'facture fournisseur sans ICE exploitable'."""
    content = _png()
    pipeline.registry[content] = OCR_SANS_ICE
    file = DocumentFile(filename="IMG_20260824.png", content=content, source="attachment")
    outcome = pipeline.process_document(
        file, {"messageId": "m-img", "subject": "Photo", "sender": "x@example.ma"},
        attachment_id="att-img", source_url="https://example.invalid/i.png",
    )
    assert pipeline.fake_vision.appels == ["terra", "sol"]
    assert outcome.action == ACTION_AUTO, outcome.reasons
    assert outcome.document.emetteur_ice == "009999000004401"
    assert outcome.document.text_source == "vision:sol"


def test_une_copie_renvoyee_ne_declenche_plus_aucun_appel_modele(pipeline):
    """Cache par empreinte : les MEMES octets renvoyes dans un autre email
    (copie renommee) ne coutent plus aucun appel Terra/Sol - le contenu a
    deja ete lu, la deduplication tranche. Avant le correctif, la copie
    etait relue par les modeles avant d'etre reconnue comme doublon."""
    content = _png()
    pipeline.registry[content] = OCR_SANS_ICE
    premier = DocumentFile(filename="IMG_20260824.png", content=content, source="attachment")
    pipeline.process_document(
        premier, {"messageId": "m-img", "subject": "Photo", "sender": "x@example.ma"},
        attachment_id="att-img", source_url="https://example.invalid/i.png",
    )
    appels_apres_premier = list(pipeline.fake_vision.appels)

    copie = DocumentFile(filename="IMG_rappel_v2.png", content=content, source="attachment")
    outcome = pipeline.process_document(
        copie, {"messageId": "m-rappel", "subject": "Rappel", "sender": "x@example.ma"},
        attachment_id="att-rappel", source_url="https://example.invalid/i2.png",
    )
    assert pipeline.fake_vision.appels == appels_apres_premier, (
        "la copie ne doit declencher AUCUN nouvel appel modele"
    )
    assert outcome.action != ACTION_AUTO or outcome.reasons  # doublon reconnu, rien d'ecrit
