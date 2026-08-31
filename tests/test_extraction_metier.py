"""Causes racines des quarantaines du test multi-entreprises reel.

Quatre familles d'echec observees en production sur les dossiers de
demonstration (40 quarantaines pour 21 attendues) :

  A. factures de VENTE : le bloc "DESTINATAIRE" des pieces reelles n'etait
     pas un libelle de partie reconnu -> "destinataire illisible ou absent" ;
  B. AVOIRS fournisseurs : "Document d'origine : FA-..." n'etait pas un
     libelle de facture liee reconnu -> "avoir sans facture d'origine
     identifiable" ; et l'existence de l'origine chez CE tenant n'etait
     jamais verifiee ;
  C. RECUS : numero imprime en tete sans libelle, "Date d'encaissement" et
     "Facture concernee" non reconnus -> "numero du document illisible ou
     absent" ;
  D. IMAGES difficiles : OCR en une seule passe, sans redressement ni
     nettoyage.

Chaque test de ce fichier echoue sur l'arbre AVANT correctif : ce sont les
fixtures textuelles EXACTES des PDF de demonstration (memes libelles, memes
blocs) qui les pilotent. Aucun cas particulier sur un nom de fichier, une
societe ou un montant ; aucun seuil abaisse.
"""
from __future__ import annotations

import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app import doc_store as store
from app.attachments import DocumentFile
from app.db import init_db
from app.doc_extract import extract_document
from app.doc_pipeline import DocumentPipeline
from app.doc_policy import (
    ACTION_AUTO,
    ACTION_REVIEW,
    DecisionContext,
    decide,
)
from app.doc_types import (
    PAYMENT_RECEIPT,
    PURCHASE_INVOICE,
    SALES_INVOICE,
    SUPPLIER_CREDIT_NOTE,
)
from workbook_fake import FakeWorkbook

ICE_ATLAS = "009999000000001"
ICE_NOVA = "009999000002201"
ICE_MAGHREB = "009999000001102"

# Couche texte reelle (structure et libelles) d'une facture de VENTE du
# dossier de demonstration : blocs EMETTEUR / DESTINATAIRE, ICE de chaque
# partie, totaux en fin de page.
FV_TEXT = f"""FACTURE CLIENT
FV-ATD-2026-001
Page 1/1
EMETTEUR
ATLAS DIGITAL DEMO SARL
12 avenue Demo, Casablanca, Maroc - adresse fictive
ICE : {ICE_ATLAS}
DESTINATAIRE
NOVA RETAIL DEMO SARL
Casablanca - adresse fictive
ICE : {ICE_NOVA}
NUMERO
FV-ATD-2026-001
DATE
2026-08-04
ECHEANCE
2026-09-03
Prestations de services - forfait mensuel
Total HT
8 000.00 MAD
TVA 20 %
1 600.00 MAD
Total TTC
9 600.00 MAD
Reference de paiement : FV-ATD-2026-001
DOCUMENT DE DEMONSTRATION - SANS VALEUR FISCALE
"""

AVF_TEXT = f"""AVOIR FOURNISSEUR
AVF-ATD-2026-001
Page 1/1
EMETTEUR
MAGHREB CLOUD DEMO SARL
Rabat - adresse fictive
ICE : {ICE_MAGHREB}
DESTINATAIRE
ATLAS DIGITAL DEMO SARL
Casablanca - adresse fictive
ICE : {ICE_ATLAS}
NUMERO
AVF-ATD-2026-001
DATE
2026-08-20
Document d'origine : FA-ATD-2026-002
Regularisation sur prestation cloud
Total HT
400.00 MAD
TVA 20 %
80.00 MAD
Total TTC
480.00 MAD
DOCUMENT DE DEMONSTRATION - SANS VALEUR FISCALE
"""

REC_TEXT = """RECU DE PAIEMENT
REC-ATD-2026-001
Page 1/1
ATLAS DIGITAL DEMO SARL
Recu de : NOVA RETAIL DEMO SARL
Date d'encaissement : 2026-08-12
Facture concernee : FV-ATD-2026-001
Mode de reglement : virement bancaire
Montant
9 600.00 MAD
DOCUMENT DE DEMONSTRATION - SANS VALEUR FISCALE
"""

FA_TEXT = f"""FACTURE FOURNISSEUR
FA-ATD-2026-002
Page 1/1
FOURNISSEUR
MAGHREB CLOUD DEMO SARL
Rabat - adresse fictive
ICE : {ICE_MAGHREB}
CLIENT
ATLAS DIGITAL DEMO SARL
Casablanca - adresse fictive
ICE : {ICE_ATLAS}
Numero de facture : FA-ATD-2026-002
Date de facture : 2026-08-05
Hebergement cloud - aout
Total HT
2 400.00 MAD
TVA 20 %
480.00 MAD
Total TTC
2 880.00 MAD
"""


def _extraire(texte: str, company: str = "ATLAS DIGITAL DEMO"):
    return extract_document([texte], company=company)


# === Famille A : identification vendeur/client d'une facture de vente ====

def test_le_bloc_destinataire_d_une_vente_reelle_est_lu_avec_son_ice():
    doc = _extraire(FV_TEXT)
    assert doc.doc_type == SALES_INVOICE
    assert doc.destinataire == "NOVA RETAIL DEMO SARL"
    assert doc.destinataire_ice == ICE_NOVA
    assert doc.emetteur == "ATLAS DIGITAL DEMO SARL"
    assert doc.emetteur_ice == ICE_ATLAS
    assert "destinataire" not in doc.missing


def test_une_vente_reelle_complete_est_comptabilisable(monkeypatch):
    doc = _extraire(FV_TEXT)
    decision = decide(doc, DecisionContext(
        today=date(2026, 8, 30),
        allowed_vat_rates=(Decimal("20"),),
        company_ice=ICE_ATLAS,
        credit_note_targets=None,
    ))
    assert decision.action == ACTION_AUTO, decision.reasons


def test_l_ice_du_bloc_emetteur_ne_deborde_pas_sur_le_destinataire():
    """La recherche d'ICE du bloc EMETTEUR doit s'arreter a la ligne
    DESTINATAIRE : sans cette borne, l'ICE du client peut etre attribue au
    vendeur quand l'adresse du vendeur ne porte pas d'ICE."""
    sans_ice_emetteur = FV_TEXT.replace(f"ICE : {ICE_ATLAS}\n", "", 1)
    doc = _extraire(sans_ice_emetteur)
    assert doc.emetteur_ice is None
    assert doc.destinataire_ice == ICE_NOVA


def test_vente_dont_le_tenant_est_le_destinataire_part_en_quarantaine():
    """Le sens du document se verifie contre l'identite legale du tenant,
    jamais contre le nom du fichier : une 'vente' ou l'entreprise est en
    realite le DESTINATAIRE est contradictoire."""
    doc = _extraire(FV_TEXT)
    decision = decide(doc, DecisionContext(
        today=date(2026, 8, 30),
        allowed_vat_rates=(Decimal("20"),),
        company_ice=ICE_NOVA,  # le tenant est le client, pas le vendeur
        credit_note_targets=None,
    ))
    assert decision.action == ACTION_REVIEW
    assert any("orientation contradictoire" in r for r in decision.reasons)


def test_achat_dont_le_tenant_est_l_emetteur_part_en_quarantaine():
    doc = _extraire(FA_TEXT)
    assert doc.doc_type == PURCHASE_INVOICE
    decision = decide(doc, DecisionContext(
        today=date(2026, 8, 30),
        allowed_vat_rates=(Decimal("20"),),
        company_ice=ICE_MAGHREB,  # le tenant est le fournisseur emetteur
        credit_note_targets=None,
    ))
    assert decision.action == ACTION_REVIEW
    assert any("orientation contradictoire" in r for r in decision.reasons)


def test_document_dont_aucune_partie_n_est_le_tenant_part_en_quarantaine():
    doc = _extraire(FV_TEXT)
    decision = decide(doc, DecisionContext(
        today=date(2026, 8, 30),
        allowed_vat_rates=(Decimal("20"),),
        company_ice="000000000000099",  # ICE d'une TOUT AUTRE societe
        credit_note_targets=None,
    ))
    assert decision.action == ACTION_REVIEW
    assert any("aucune des deux parties" in r for r in decision.reasons)


def test_sans_ice_tenant_le_controle_d_orientation_est_inapplicable():
    """Compatibilite mono-entreprise : sans ICE configure, aucun nouveau
    motif de quarantaine n'apparait."""
    doc = _extraire(FV_TEXT)
    decision = decide(doc, DecisionContext(
        today=date(2026, 8, 30),
        allowed_vat_rates=(Decimal("20"),),
        credit_note_targets=None,
    ))
    assert decision.action == ACTION_AUTO, decision.reasons


# === Famille B : avoirs fournisseurs =====================================

def test_le_document_d_origine_d_un_avoir_reel_est_extrait():
    doc = _extraire(AVF_TEXT)
    assert doc.doc_type == SUPPLIER_CREDIT_NOTE
    assert doc.facture_liee == "FA-ATD-2026-002"


def test_un_avoir_sans_origine_reste_en_quarantaine():
    sans_origine = AVF_TEXT.replace("Document d'origine : FA-ATD-2026-002\n", "")
    doc = _extraire(sans_origine)
    assert doc.facture_liee is None
    decision = decide(doc, DecisionContext(
        today=date(2026, 8, 30), allowed_vat_rates=(Decimal("20"),),
        credit_note_targets=None,
    ))
    assert decision.action == ACTION_REVIEW
    assert any("sans facture d'origine" in r for r in decision.reasons)


def test_un_avoir_dont_l_origine_est_introuvable_part_en_quarantaine():
    """La reference est lue mais AUCUNE facture de cette societe ne la
    porte : jamais de rattachement par ressemblance."""
    doc = _extraire(AVF_TEXT)
    decision = decide(doc, DecisionContext(
        today=date(2026, 8, 30), allowed_vat_rates=(Decimal("20"),),
        credit_note_targets=0,
    ))
    assert decision.action == ACTION_REVIEW
    assert any("introuvable dans cette societe" in r for r in decision.reasons)


def test_un_avoir_dont_l_origine_existe_une_fois_est_comptabilisable():
    doc = _extraire(AVF_TEXT)
    decision = decide(doc, DecisionContext(
        today=date(2026, 8, 30), allowed_vat_rates=(Decimal("20"),),
        credit_note_targets=1, company_ice=ICE_ATLAS,
    ))
    assert decision.action == ACTION_AUTO, decision.reasons


def test_les_montants_de_l_avoir_sont_signes_une_seule_fois():
    doc = _extraire(AVF_TEXT)
    assert doc.montant_ht.value == Decimal("-400.00")
    assert doc.montant_tva.value == Decimal("-80.00")
    assert doc.montant_ttc.value == Decimal("-480.00")


# === Famille C : recus sans numero exploitable ===========================

def test_le_recu_reel_livre_numero_date_montant_et_facture_liee():
    doc = _extraire(REC_TEXT)
    assert doc.doc_type == PAYMENT_RECEIPT
    # Numero imprime en tete, seul sur sa ligne, sans libelle.
    assert doc.numero == "REC-ATD-2026-001"
    assert doc.date_document == date(2026, 8, 12)
    assert doc.facture_liee == "FV-ATD-2026-001"
    assert doc.montant_paye is not None
    assert doc.montant_paye.value == Decimal("9600.00")
    assert doc.destinataire == "NOVA RETAIL DEMO SARL"
    assert doc.missing == []


def test_le_titre_du_recu_ne_devient_jamais_un_payeur():
    doc = _extraire(REC_TEXT)
    assert doc.destinataire != "PAIEMENT"
    assert (doc.emetteur or "") != "PAIEMENT"


def test_un_recu_a_deux_montants_differents_ne_choisit_pas_tout_seul():
    """Le repli 'montant unique' ne s'applique JAMAIS quand le document
    porte deux valeurs distinctes sans libelle de montant paye."""
    ambigu = REC_TEXT.replace(
        "Montant\n9 600.00 MAD\n", "Montant\n9 600.00 MAD\nAcompte\n5 000.00 MAD\n"
    )
    doc = _extraire(ambigu)
    assert doc.montant_paye is None
    assert "montant_paye" in doc.missing


def test_une_date_seule_en_tete_ne_devient_jamais_un_numero():
    texte = REC_TEXT.replace("REC-ATD-2026-001\n", "2026-08-12\n", 1)
    doc = _extraire(texte)
    assert doc.numero != "2026-08-12"


# === Pipeline complet : avoir, recu, identifiant interne =================

@pytest.fixture
def db_path():
    from app import tenancy

    path = tempfile.mktemp(suffix=".db")
    init_db(path)
    store.ensure_schema(path)
    tenancy.migrate_to_multi_tenant(path)
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def pipeline(db_path, monkeypatch):
    import app.doc_pipeline as module

    workbook = FakeWorkbook()
    registry: dict[bytes, str] = {}

    def fake_read(content, company="X BLASTE", ocr=True):
        return extract_document([registry[content]], company=company)

    monkeypatch.setattr(module, "extract_from_pdf_bytes", fake_read)
    pipe = DocumentPipeline(
        workbook, db_path=db_path, chat_id=999653395,
        spreadsheet_id="sheet-test", company="ATLAS DIGITAL DEMO",
        allowed_vat_rates=(Decimal("20"),),
        company_id="atlas-demo", company_ice=ICE_ATLAS,
        today=lambda: date(2026, 8, 30),
    )
    pipe.registry = registry  # type: ignore[attr-defined]
    return pipe


def _run(pipeline, texte: str, nom: str, *, message_id: str = "m-1"):
    content = f"%PDF-{nom}".encode()
    pipeline.registry[content] = texte
    file = DocumentFile(filename=f"{nom}.pdf", content=content, source="attachment")
    return pipeline.process_document(
        file,
        {"messageId": message_id, "subject": "Dossier", "sender": "x@example.ma"},
        attachment_id=f"att-{nom}",
        source_url="https://example.invalid/f.pdf",
    )


def test_pipeline_vente_puis_recu_rapproche_et_solde(pipeline):
    vente = _run(pipeline, FV_TEXT, "fv-001")
    assert vente.action == ACTION_AUTO, vente.reasons
    recu = _run(pipeline, REC_TEXT, "rec-001", message_id="m-2")
    assert recu.action == ACTION_AUTO, recu.reasons
    assert any("soldee" in r for r in recu.reasons)


def test_pipeline_avoir_avec_origine_comptabilisee_passe(pipeline):
    achat = _run(pipeline, FA_TEXT, "fa-002")
    assert achat.action == ACTION_AUTO, achat.reasons
    avoir = _run(pipeline, AVF_TEXT, "avf-001", message_id="m-3")
    assert avoir.action == ACTION_AUTO, avoir.reasons


def test_pipeline_avoir_sans_origine_connue_part_en_quarantaine(pipeline):
    avoir = _run(pipeline, AVF_TEXT, "avf-seul")
    assert avoir.action == ACTION_REVIEW
    assert any("introuvable dans cette societe" in r for r in avoir.reasons)


def test_identifiant_interne_d_un_recu_sans_numero_est_deterministe(pipeline, db_path, monkeypatch):
    """Un recu vraiment sans numero recoit un identifiant interne derive de
    (entreprise, email, membre, empreinte) : meme piece, meme identifiant,
    a chaque calcul - et il n'est jamais presente comme un numero legal."""
    sans_numero = REC_TEXT.replace("REC-ATD-2026-001\n", "", 1)
    doc = _extraire(sans_numero)
    assert doc.numero is None
    assert "numero" in doc.missing

    _run(pipeline, FV_TEXT, "fv-001")
    outcome = _run(pipeline, sans_numero, "rec-sans-numero", message_id="m-9")
    assert outcome.action == ACTION_AUTO, outcome.reasons
    premier = outcome.document.numero_interne
    assert premier and premier.startswith("REC-INT-")
    assert outcome.stable_id == premier

    # Meme evenement rejoue => meme identifiant (aucun alea, aucun horodatage).
    import hashlib
    graine = "|".join(("atlas-demo", "m-9", "",
                       hashlib.sha256(b"%PDF-rec-sans-numero").hexdigest()))
    attendu = "REC-INT-" + hashlib.sha256(graine.encode()).hexdigest()[:10].upper()
    assert premier == attendu


def test_recu_sans_numero_et_sans_facture_correspondante_reste_en_quarantaine(pipeline):
    sans_numero = REC_TEXT.replace("REC-ATD-2026-001\n", "", 1)
    outcome = _run(pipeline, sans_numero, "rec-orphelin")
    assert outcome.action == ACTION_REVIEW
    assert any("aucune facture" in r for r in outcome.reasons)


# === Famille D : OCR multi-passes borne ==================================

class _FakeTesseract:
    """Moteur OCR simule : l'image brute rend du bruit, l'image nettoyee
    (niveaux de gris) rend le texte - c'est exactement le comportement
    observe sur les photos de factures difficiles."""

    def __init__(self, *, osd_angle: int = 0):
        self.calls: list[tuple[str, tuple[int, int]]] = []
        self.osd_angle = osd_angle
        self.osd_requests = 0

    def image_to_osd(self, image):
        self.osd_requests += 1
        return f"Rotate: {self.osd_angle}\nOrientation confidence: 5.0"

    def image_to_string(self, image, lang="eng"):
        self.calls.append((image.mode, image.size))
        if image.mode == "L":
            return "FACTURE\nNumero de facture : FA-IMG-2026-009\nTotal TTC\n4 320.00 MAD"
        return "#@!~\n%%"


def _petite_image_png() -> bytes:
    import io

    from PIL import Image

    img = Image.new("RGB", (400, 200), "white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def test_l_ocr_garde_la_meilleure_passe_pas_la_premiere(monkeypatch):
    import sys

    from app.doc_extract import read_image_text

    fake = _FakeTesseract()
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    texte = read_image_text(_petite_image_png())
    assert "FA-IMG-2026-009" in texte
    # Les deux familles de passes ont bien ete tentees (brute + nettoyees),
    # dans une limite bornee.
    modes = {mode for mode, _ in fake.calls}
    assert modes == {"RGB", "L"}
    assert len(fake.calls) <= 3


def test_une_image_tournee_est_redressee_avant_lecture(monkeypatch):
    import sys

    from app.doc_extract import read_image_text

    fake = _FakeTesseract(osd_angle=90)
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    read_image_text(_petite_image_png())
    assert fake.osd_requests == 1
    # L'image source fait 400x200 : redressee de 90 degres, la premiere
    # passe lit une image 200x400.
    assert fake.calls[0][1] == (200, 400)


def test_un_echec_de_toutes_les_passes_reste_une_erreur_claire(monkeypatch):
    import sys

    from app.doc_extract import DocumentExtractError, read_image_text

    class _Casse:
        def image_to_osd(self, image):
            raise RuntimeError("osd indisponible")

        def image_to_string(self, image, lang="eng"):
            raise RuntimeError("tesseract absent")

    monkeypatch.setitem(sys.modules, "pytesseract", _Casse())
    with pytest.raises(DocumentExtractError):
        read_image_text(_petite_image_png())
