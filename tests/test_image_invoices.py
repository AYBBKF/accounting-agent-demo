"""Factures photographiees (PNG / JPEG) : selection, ingestion, OCR, politique.

Le contrat produit est explicite : une image envoyee directement doit etre
traitee EXACTEMENT comme un PDF -

  - OCR puis extraction complete par le meme moteur ;
  - comptabilisation automatique UNIQUEMENT si la confiance atteint le seuil,
    que les controles comptables passent et qu'aucune anomalie n'est detectee ;
  - sinon, EXACTEMENT une ligne dans `21_A_VERIFIER` avec une raison claire ;
  - une image illisible ou corrompue laisse elle aussi une ligne tracable,
    jamais un abandon silencieux.

Les octets d'origine, l'empreinte sha256, la tracabilite, la deduplication et
les limites de securite (taille, pixels, images decompressees) sont preserves.

L'extraction reelle (OCR) est deterministe et couteuse : les tests
d'integration la remplacent par une table octets->texte, exactement comme les
tests PDF remplacent `extract_from_pdf_bytes`. Les GARDE-FOUS d'image
(pixels, corruption) et un round-trip OCR reel sont testes SANS ce remplacement.
"""
import io
import tempfile
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app import doc_store as store
from app.attachments import (
    MAX_IMAGE_PIXELS,
    collect_documents,
    content_mimetype,
    is_image,
    is_pdf,
)
from app.db import init_db
from app.doc_extract import (
    DocumentExtractError,
    extract_document,
    extract_from_image_bytes,
    read_image_text,
)
from app.doc_policy import ACTION_AUTO, ACTION_REVIEW
from test_mail_worker import ACHAT, DEVIS, VENTE, FakeMailWorker, pdf_bytes, text_of, zip_of
from workbook_fake import FakeWorkbook

CHAT_ID = 999653395
ANOMALIE = "09_FACTURE_ANOMALIE_FAC-TEST-2026-003_VALIDATION_REQUISE"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


# --- fabrique d'images reelles (signature valide, octets uniques) ---------

def image_bytes(marker: str, fmt: str = "PNG", size=(320, 160)) -> bytes:
    """Une vraie image PNG/JPEG portant un marqueur : signature valide, et
    des octets qui different d'un marqueur a l'autre (donc un sha distinct)."""
    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).text((8, 70), marker, fill="black")
    buffer = io.BytesIO()
    img.save(buffer, format=fmt)
    return buffer.getvalue()


# === 1. selection binaire (unitaire) ======================================

def test_is_image_recognizes_png_and_jpeg_and_nothing_else():
    assert is_image(image_bytes("x", "PNG"))
    assert is_image(image_bytes("x", "JPEG"))
    assert not is_image(b"%PDF-1.4 ...")
    assert not is_image(b"PK\x03\x04 ...")
    assert not is_image(b"GIF89a")            # GIF hors contrat
    assert not is_image(b"")


def test_collect_documents_accepts_a_direct_png_or_jpg_preserving_bytes():
    for fmt in ("PNG", "JPEG"):
        raw = image_bytes("facture", fmt)
        report = collect_documents("facture.img", raw)
        assert len(report.files) == 1
        doc = report.files[0]
        assert doc.content == raw               # octets ORIGINAUX intacts
        assert doc.source == "attachment"
        assert doc.sha256 == __import__("hashlib").sha256(raw).hexdigest()
        assert report.rejected == []


def test_collect_documents_accepts_images_inside_a_zip():
    archive = zip_of({
        "achat.pdf": pdf_bytes(ACHAT),
        "photos/recu.jpg": image_bytes("recu", "JPEG"),
        "photos/note.txt": b"ceci n'est pas exploitable",
    })
    report = collect_documents("lot.zip", archive)
    kinds = sorted(is_pdf(f.content) and "pdf" or "image" for f in report.files)
    assert kinds == ["image", "pdf"]
    rejected = " ".join(f"{n} {r}" for n, r in report.rejected)
    assert "note.txt" in rejected


def test_content_mimetype_follows_the_signature_not_the_name():
    assert content_mimetype(pdf_bytes(ACHAT)) == "application/pdf"
    assert content_mimetype(image_bytes("x", "PNG")) == "image/png"
    assert content_mimetype(image_bytes("x", "JPEG")) == "image/jpeg"


# === 2. garde-fous d'image (unitaire, OCR reel) ===========================

def test_an_oversized_image_is_refused_with_a_clear_reason(monkeypatch):
    """Bombe de decompression : plafond de pixels applique AVANT tout OCR."""
    monkeypatch.setattr("app.attachments.MAX_IMAGE_PIXELS", 100)
    raw = image_bytes("grande", "PNG", size=(50, 50))   # 2500 px > 100
    with pytest.raises(DocumentExtractError) as excinfo:
        extract_from_image_bytes(raw)
    assert "pixels" in str(excinfo.value)


def test_a_corrupt_image_raises_instead_of_being_dropped():
    corrompue = PNG_MAGIC + b"ceci n'est pas une image valide"
    assert is_image(corrompue)                          # bien routee vers l'OCR
    with pytest.raises(DocumentExtractError):
        extract_from_image_bytes(corrompue)


def test_read_image_text_runs_on_a_real_image_without_raising():
    """Fumee : la chaine PIL -> conversion -> tesseract (repli eng) fonctionne
    sur une vraie image. On n'exige pas la PRECISION de l'OCR, seulement que
    le tuyau ne casse pas. Ignore si le moteur OCR est indisponible ici."""
    raw = image_bytes("TOTAL TTC 1200 MAD", "PNG", size=(600, 200))
    try:
        text = read_image_text(raw)
    except DocumentExtractError as exc:
        pytest.skip(f"moteur OCR indisponible dans cet environnement: {exc}")
    assert isinstance(text, str)


# === integration : un vrai cycle du worker sur des emails simules =========

@pytest.fixture
def db_path():
    path = tempfile.mktemp(suffix=".db")
    init_db(path)
    store.ensure_schema(path)
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def worker(db_path, monkeypatch):
    import app.doc_pipeline as module

    pdf_registry = {pdf_bytes(name): text_of(name) for name in (ACHAT, VENTE, DEVIS)}
    image_registry: dict[bytes, str] = {}
    corrupt: set[bytes] = set()

    def fake_pdf(content, company="X BLASTE", ocr=True):
        if content not in pdf_registry:
            raise ValueError("PDF illisible")
        return extract_document([pdf_registry[content]], company=company)

    def fake_image(content, company="X BLASTE", ocr=True):
        if content in corrupt or content not in image_registry:
            raise DocumentExtractError("image illisible ou corrompue.")
        return extract_document(
            [image_registry[content]], company=company, text_source="ocr"
        )

    monkeypatch.setattr(module, "extract_from_pdf_bytes", fake_pdf)
    monkeypatch.setattr(module, "extract_from_image_bytes", fake_image)

    built = FakeMailWorker(FakeWorkbook(), db_path)
    built.db_path = db_path
    built.image_registry = image_registry
    built.corrupt = corrupt
    return built


def _booked(worker):
    return worker.workbook.writes_to("05_FACTURES_ACHATS")


def _quarantined(worker):
    return store.list_quarantined(worker.db_path, CHAT_ID)


def test_a_photographed_invoice_is_booked_exactly_once(worker):
    """Photo d'une facture propre : OCR -> extraction -> UNE ecriture, zero
    quarantaine. Le seul chemin positif, prouve sur une vraie image."""
    raw = image_bytes("achat-propre", "JPEG")
    worker.image_registry[raw] = text_of(ACHAT)
    worker.add_message(
        "m-photo-ok", internal_date=worker.moment(0),
        attachments={"facture_achat.jpg": raw},
    )
    summary = worker.process_once()[0]

    assert len(summary.outcomes) == 1
    outcome = summary.outcomes[0]
    assert outcome.action == ACTION_AUTO
    assert outcome.doc_type == "facture_achat"
    assert outcome.accounting
    assert _quarantined(worker) == []
    assert worker.workbook.writes_to("21_A_VERIFIER") == []


def test_a_photographed_invoice_with_an_anomaly_is_quarantined_once(worker):
    """Photo lisible mais anomalie comptable : EXACTEMENT une ligne dans
    `21_A_VERIFIER`, aucune ecriture comptable, une raison claire."""
    raw = image_bytes("achat-anomalie", "PNG")
    worker.image_registry[raw] = text_of(ANOMALIE)
    worker.add_message(
        "m-photo-anomalie", internal_date=worker.moment(0),
        attachments={"facture_photo.png": raw},
    )
    summary = worker.process_once()[0]

    assert len(summary.outcomes) == 1
    outcome = summary.outcomes[0]
    assert outcome.action == ACTION_REVIEW
    assert _booked(worker) == []
    assert len(_quarantined(worker)) == 1
    assert outcome.reasons, "une quarantaine doit porter une raison lisible"


def test_a_corrupt_photo_leaves_one_traceable_review_line(worker):
    """Image illisible : jamais abandonnee en silence. Une ligne tracable en
    quarantaine, exactement comme un PDF illisible."""
    corrompue = PNG_MAGIC + b"octets illisibles"
    worker.corrupt.add(corrompue)
    worker.add_message(
        "m-photo-corrompue", internal_date=worker.moment(0),
        attachments={"scan_flou.png": corrompue},
    )
    summary = worker.process_once()[0]

    assert len(summary.outcomes) == 1
    outcome = summary.outcomes[0]
    assert outcome.action == ACTION_REVIEW
    assert outcome.pending_review or outcome.error
    assert _booked(worker) == []
    assert len(_quarantined(worker)) == 1


def test_the_same_photo_resent_is_not_duplicated(worker):
    """Deduplication par empreinte : les memes octets d'image renvoyes sous un
    autre nom ne creent pas une seconde fiche."""
    raw = image_bytes("achat-anomalie", "PNG")
    worker.image_registry[raw] = text_of(ANOMALIE)
    worker.add_message(
        "m-photo-1", internal_date=worker.moment(0),
        attachments={"photo_v1.png": raw},
    )
    worker.process_once()
    assert len(_quarantined(worker)) == 1

    worker.add_message(
        "m-photo-2", internal_date=worker.moment(50),
        attachments={"photo_renommee.png": raw},   # memes octets, autre nom
    )
    worker.process_once()
    # Aucune seconde ligne : la fiche renvoyee est rattachee a sa canonique.
    assert len(_quarantined(worker)) == 1
    assert _booked(worker) == []


def test_a_zip_mixing_a_pdf_and_a_photo_processes_both(worker):
    """Un lot mensuel melange PDF et photos : les deux formes sont traitees,
    sans troncature."""
    photo = image_bytes("vente-photo", "JPEG")
    worker.image_registry[photo] = text_of(VENTE)
    worker.add_message(
        "m-zip-mixte", internal_date=worker.moment(0),
        attachments={"lot.zip": zip_of({
            "achat.pdf": pdf_bytes(ACHAT),
            "photos/vente.jpg": photo,
        })},
    )
    summary = worker.process_once()[0]

    assert len(summary.outcomes) == 2
    assert {o.doc_type for o in summary.outcomes} == {"facture_achat", "facture_vente"}
