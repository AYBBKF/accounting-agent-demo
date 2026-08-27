"""Les deux emails reels du 26 aout 2026, rejoues a l'identique.

Incident reproduit ici, dans l'ordre ou il s'est produit :

  1. un email portait un ZIP de 38 PDF ; la limite de 25 fichiers en a
     laisse 13 non lus, sans que personne ne le voie ;
  2. les 38 MEMES PDF ont ete renvoyes quelques minutes plus tard, en
     pieces jointes separees ;
  3. la deduplication ne regardait que les documents COMPTABILISES : les
     documents en quarantaine sont repartis de zero et ont ajoute une
     seconde ligne rouge chacun dans `21_A_VERIFIER`.

Le test verifie les trois corrections ensemble, parce que c'est ensemble
qu'elles ont echoue. Il verifie aussi ce qui doit rester vrai : aucune
fiche SQLite supprimee, et une comptabilite qui ne bouge pas d'un pouce
au second envoi.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app import doc_store as store
from app.doc_extract import extract_document
from app.doc_policy import ACTION_AUTO, ACTION_DUPLICATE, ACTION_REVIEW
from app.review_sheet import TAB_REVIEW

from test_mail_worker import FakeMailWorker, pdf_bytes, zip_of
from workbook_fake import FakeWorkbook

# 38 documents : 34 factures d'achat saines et 4 anomalies, une par regle
# de refus ajoutee. La repartition reproduit le lot reel - une majorite de
# pieces ordinaires, quelques cas qui doivent s'arreter.
TOTAL = 38
ANOMALIES = 5
AUJOURD_HUI = date(2026, 8, 26)

FOURNISSEURS = [
    ("ATLAS BUREAU SARL", "002345678000043"),
    ("TECH OFFICE SARL", "003202020000092"),
    ("OMEGA FOURNITURES SARL", "003711223000091"),
    ("BETA SERVICES SARL", "003922334000051"),
]


def facture(
    numero: str,
    *,
    fournisseur: str = "ATLAS BUREAU SARL",
    ice: str = "002345678000043",
    jour: str = "22/08/2026",
    taux: str = "20",
    ht: str = "6 250.00",
    tva: str = "1 250.00",
    ttc: str = "7 500.00",
) -> str:
    """Une facture d'achat lisible, parametrable sur ce qui doit varier."""
    return f"""PACK DE TEST COMPTABLE - X BLASTE
DOCUMENT FICTIF - TEST
Fournisseur : {fournisseur}
Adresse : 18 avenue Hassan II, Casablanca, Maroc
ICE : {ice}
IF : 18765432
 FACTURE
 FOURNISSEUR
Client : X BLASTE
Adresse : 25 boulevard Zerktouni, Casablanca, Maroc
ICE : 003456789000052
Référence : {numero}
Numéro de facture
{numero}
Date
{jour}
Échéance
21/09/2026
Détail de la facture
 Désignation
 Qté
Prix unitaire HT
Montant HT
Prestation comptable
1
 {ht} MAD
 {ht} MAD
 
 Total HT
 {ht} MAD
 
 TVA {taux} %
 {tva} MAD
 
 Total TTC
 {ttc} MAD
"""


def le_lot() -> dict[str, str]:
    """Les 38 documents, nommes comme dans l'archive reelle."""
    lot: dict[str, str] = {}
    for index in range(1, TOTAL - ANOMALIES + 1):
        fournisseur, ice = FOURNISSEURS[index % len(FOURNISSEURS)]
        numero = f"FAC-ACH-2026-{600 + index}"
        lot[f"factures/{numero}.pdf"] = facture(
            numero, fournisseur=fournisseur, ice=ice
        )

    # Les quatre anomalies, une par regle. Chacune doit s'arreter en
    # quarantaine, et pour un motif nomme.
    lot["factures/FAC-ACH-2026-511.pdf"] = facture(
        "FAC-ACH-2026-511", jour="15/01/2027",                  # date future
    )
    lot["factures/FAC-ACH-2026-512.pdf"] = facture(
        "FAC-ACH-2026-512", taux="17", tva="1 062.50", ttc="7 312.50",
    )
    lot["factures/FAC-ACH-2026-513.pdf"] = facture(
        "FAC-ACH-2026-513", ht="-1 000.00", tva="-200.00", ttc="-1 200.00",
    )
    lot["factures/FAC-ACH-2026-514.pdf"] = facture(
        "FAC-ACH-2026-514", fournisseur="SARL",                 # tiers generique
    )
    lot["factures/FAC-ACH-2026-515.pdf"] = facture(
        "FAC-ACH-2026-515", fournisseur="SERVICES GENERAUX MAROC", ice="",
    )
    return lot


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "demo.db")


@pytest.fixture
def worker(db_path, monkeypatch):
    import app.doc_pipeline as module

    lot = le_lot()
    registre = {pdf_bytes(chemin): texte for chemin, texte in lot.items()}

    def lecture(content, company="X BLASTE", ocr=True):
        if content not in registre:
            raise ValueError("PDF illisible")
        return extract_document([registre[content]], company=company)

    monkeypatch.setattr(module, "extract_from_pdf_bytes", lecture)
    w = FakeMailWorker(
        FakeWorkbook(), db_path,
        allowed_vat_rates=(20, 10, 7, 0),
    )
    # Horloge FIGEE : sans cela, "datee dans le futur" cesserait d'etre
    # vrai le 15 janvier 2027 et le test deviendrait un faux positif.
    w.pipeline._today = lambda: AUJOURD_HUI
    w.lot = lot
    return w


def envoyer_en_zip(w, message_id: str = "1a03f0572c74a0d2") -> None:
    w.add_message(
        message_id, internal_date=w.moment(0),
        attachments={"lot-aout-2026.zip": zip_of(
            {chemin: pdf_bytes(chemin) for chemin in w.lot}
        )},
        subject="Comptabilite aout 2026 - archive",
    )


def cycles(w, nombre: int = 4) -> list:
    """Plusieurs cycles Gmail, comme en production.

    Un cycle ne traite qu'un nombre borne d'emails : attendre que tout
    tienne dans un seul passage reviendrait a tester une configuration qui
    n'existe pas. On collecte donc les bilans de plusieurs tours.
    """
    tous = []
    for _ in range(nombre):
        tous.extend(w.process_once())
    return tous


def resume_de(resumes, message_id: str):
    """Le bilan d'UN email precis.

    On ne prend jamais "le dernier" : l'ordre des cycles Gmail n'est pas un
    contrat, et un test qui en depend finit par verifier autre chose que ce
    qu'il annonce.
    """
    trouve = [r for r in resumes if r.message_id == message_id]
    assert trouve, f"aucun bilan pour {message_id}"
    return trouve[0]


def envoyer_separement(w, message_id: str = "1a03f0a7e54d98b9") -> None:
    w.add_message(
        message_id, internal_date=w.moment(60),
        attachments={
            Path(chemin).name: pdf_bytes(chemin) for chemin in w.lot
        },
        subject="Comptabilite aout 2026 - pieces separees",
    )


# === 1. le ZIP : 38 lus, pas 25 ==========================================

def test_the_zip_of_38_pdfs_is_read_whole(worker):
    """Le defaut d'origine : 13 documents perdus sans un mot."""
    envoyer_en_zip(worker)
    resume = worker.process_once()[0]

    assert len(resume.outcomes) == TOTAL
    assert resume.truncated == 0
    assert not [m for m in resume.rejected if "limite de" in m[1]]


def test_the_four_anomalies_stop_and_the_rest_is_booked(worker):
    envoyer_en_zip(worker)
    resume = worker.process_once()[0]

    quarantaine = [o for o in resume.outcomes if o.action == ACTION_REVIEW]
    comptabilises = [o for o in resume.outcomes if o.action == ACTION_AUTO]
    assert len(quarantaine) == ANOMALIES
    assert len(comptabilises) == TOTAL - ANOMALIES

    motifs = {o.numero: " | ".join(o.reasons) for o in quarantaine}
    assert "futur" in motifs["FAC-ACH-2026-511"]
    assert "TVA 17" in motifs["FAC-ACH-2026-512"]
    assert "negatif" in motifs["FAC-ACH-2026-513"]
    # "SARL" seul n'est pas une raison sociale : ce motif-la tombe AVANT
    # celui de l'ICE, et c'est le bon ordre - sans tiers credible, l'ICE
    # n'a plus d'objet.
    assert "raison sociale" in motifs["FAC-ACH-2026-514"]
    assert "ICE exploitable" in motifs["FAC-ACH-2026-515"]

    # Aucune des quatre n'a touche la comptabilite.
    ecrites = {
        r[2] for r in worker.workbook.rows("05_FACTURES_ACHATS")[1:] if len(r) > 2
    }
    for numero in motifs:
        assert numero not in ecrites


# === 2. le second envoi n'ajoute RIEN ====================================

def test_the_same_38_pdfs_sent_separately_add_nothing(worker):
    """Le coeur de l'incident : zero ecriture, zero ligne, au second envoi."""
    envoyer_en_zip(worker)
    worker.process_once()

    achats = len(worker.workbook.rows("05_FACTURES_ACHATS"))
    quarantaine = len(worker.workbook.rows(TAB_REVIEW))

    envoyer_separement(worker)
    second, _ = worker.process_message("1a03f0a7e54d98b9")

    assert len(second.outcomes) == TOTAL
    assert all(o.action == ACTION_DUPLICATE for o in second.outcomes)
    assert len(worker.workbook.rows("05_FACTURES_ACHATS")) == achats
    assert len(worker.workbook.rows(TAB_REVIEW)) == quarantaine


def test_the_secondary_records_are_kept_and_marked_superseded(worker):
    """Aucune fiche d'audit supprimee : elles sont RATTACHEES."""
    envoyer_en_zip(worker)
    worker.process_once()
    apres_zip = len(store.list_documents(worker._db_path, worker._chat_id))

    envoyer_separement(worker)
    cycles(worker)

    fiches = store.list_documents(worker._db_path, worker._chat_id)
    assert len(fiches) == apres_zip + TOTAL       # rien n'a disparu

    rattachees = [f for f in fiches if f.get("superseded_by")]
    assert len(rattachees) == TOTAL
    canoniques = {f["doc_key"] for f in fiches if not f.get("superseded_by")}
    for fiche in rattachees:
        # Un rattachement pointe toujours vers une fiche CANONIQUE, jamais
        # vers une autre fiche rattachee : sinon une chaine se formerait.
        assert fiche["superseded_by"] in canoniques


def test_only_one_review_row_per_physical_document(worker):
    envoyer_en_zip(worker)
    worker.process_once()
    envoyer_separement(worker)
    cycles(worker)

    lignes = worker.workbook.rows(TAB_REVIEW)
    numeros = [ligne[3] for ligne in lignes if len(ligne) > 3]
    assert len(numeros) == ANOMALIES
    assert len(set(numeros)) == ANOMALIES


# === 3. la stabilite dans le temps =======================================

def test_replaying_both_emails_changes_nothing(worker):
    """Trois passages de plus : le classeur ne doit plus bouger du tout."""
    envoyer_en_zip(worker)
    worker.process_once()
    envoyer_separement(worker)
    cycles(worker)

    achats = len(worker.workbook.rows("05_FACTURES_ACHATS"))
    quarantaine = len(worker.workbook.rows(TAB_REVIEW))

    for _ in range(3):
        worker.process_once()

    assert len(worker.workbook.rows("05_FACTURES_ACHATS")) == achats
    assert len(worker.workbook.rows(TAB_REVIEW)) == quarantaine


def test_the_second_email_sends_no_telegram_noise(worker):
    """Un envoi qui n'apprend rien ne doit reveiller personne.

    Deux verifications de niveaux differents, et il faut les deux : au
    niveau du document, aucun n'est notifiable ; au niveau du cycle,
    l'email ne produit meme pas de bilan - donc pas un seul appel Telegram.
    """
    envoyer_en_zip(worker)
    worker.process_once()
    envoyer_separement(worker)

    second, _ = worker.process_message("1a03f0a7e54d98b9")
    assert second.notifiable == []
    assert second.silenced == len(second.outcomes)
    assert second.should_notify is False

    # Et le cycle complet n'en parle pas non plus.
    assert not [
        r for r in cycles(worker) if r.message_id == "1a03f0a7e54d98b9"
    ]
