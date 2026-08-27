"""Les limites ZIP montent SANS relacher une seule protection.

Passer de 25 a 120 fichiers ne doit rien ouvrir d'autre. Le risque d'un
tel changement n'est pas le nombre : c'est qu'on en profite pour desserrer
la profondeur, le ratio de compression ou le volume total, et qu'une bombe
de decompression passe. Le volume total reste DELIBEREMENT a 60 Mo.

Le second bloc verifie les cinq regles de refus, une par une, au niveau du
module de decision - la ou elles doivent tenir independamment du pipeline.
"""
from __future__ import annotations

import io
import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app.attachments import (
    MAX_DEPTH,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    ZipLimits,
    extract_pdfs_from_zip,
)
from app.doc_policy import (
    ACTION_REVIEW,
    DecisionContext,
    decide,
    usable_party_name,
)
from app.doc_extract import extract_document

from test_zip_38_and_split_emails import facture


def zip_of(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


# === 1. la nouvelle limite ===============================================

def test_the_file_limit_is_raised_to_at_least_a_hundred():
    assert MAX_FILES >= 100


def test_the_total_size_limit_is_deliberately_unchanged():
    """60 Mo. C'est ce qui borne la bombe, quel que soit le nombre."""
    assert MAX_TOTAL_BYTES == 60 * 1024 * 1024


def test_a_hundred_pdfs_are_all_extracted():
    archive = zip_of({f"f{i:03d}.pdf": b"%PDF-" + str(i).encode() for i in range(100)})
    rapport = extract_pdfs_from_zip(archive)
    assert len(rapport.files) == 100
    assert rapport.truncated == 0


def test_going_over_the_limit_is_counted_not_dissolved():
    """La troncature se COMPTE : c'est ce qui la rend visible."""
    archive = zip_of({f"f{i:03d}.pdf": b"%PDF-" + str(i).encode() for i in range(10)})
    rapport = extract_pdfs_from_zip(archive, limits=ZipLimits(max_files=4))
    assert len(rapport.files) == 4
    assert rapport.truncated == 6


# === 2. les protections, inchangees ======================================

def test_zip_slip_is_still_refused():
    archive = zip_of({"../../etc/passwd.pdf": b"%PDF-x"})
    rapport = extract_pdfs_from_zip(archive)
    assert rapport.files == []
    assert any("non sur" in motif for _, motif in rapport.rejected)


def test_an_absolute_path_is_still_refused():
    archive = zip_of({"/etc/shadow.pdf": b"%PDF-x"})
    rapport = extract_pdfs_from_zip(archive)
    assert rapport.files == []


def test_a_compressible_bomb_is_stopped_by_the_ratio():
    """Des zeros se compressent a l'infini : c'est le ratio qui les arrete."""
    gros = b"%PDF" + b"0" * (14 * 1024 * 1024)
    archive = zip_of({f"gros{i}.pdf": gros for i in range(8)})
    rapport = extract_pdfs_from_zip(archive)
    assert rapport.files == []
    assert any("compression" in motif for _, motif in rapport.rejected)


def test_the_total_size_still_stops_an_incompressible_flood():
    """Des octets INCOMPRESSIBLES passent le ratio : reste le volume total.

    C'est la protection que le passage a 120 fichiers aurait pu diluer, et
    c'est pour cela qu'on ne l'a pas relevee : 120 fichiers de 14 Mo ne
    peuvent toujours pas depasser 60 Mo decompresses.
    """
    import os

    gros = b"%PDF" + os.urandom(14 * 1024 * 1024)
    archive = zip_of({f"gros{i}.pdf": gros[:] for i in range(8)})
    rapport = extract_pdfs_from_zip(archive)
    assert any("taille totale" in motif for _, motif in rapport.rejected)
    assert len(rapport.files) < 8


def test_a_file_over_the_unit_size_is_still_refused():
    archive = zip_of({"enorme.pdf": b"%PDF" + b"0" * (16 * 1024 * 1024)})
    rapport = extract_pdfs_from_zip(archive)
    assert rapport.files == []


def test_nesting_depth_is_still_bounded():
    assert MAX_DEPTH == 2
    profond = zip_of({"a.pdf": b"%PDF-a"})
    for _ in range(MAX_DEPTH + 2):
        profond = zip_of({"suivant.zip": profond})
    rapport = extract_pdfs_from_zip(profond)
    assert rapport.files == []
    assert any("profondeur" in motif for _, motif in rapport.rejected)


def test_a_non_pdf_member_is_still_refused_by_signature():
    archive = zip_of({"faux.pdf": b"MZ ceci est un executable"})
    rapport = extract_pdfs_from_zip(archive)
    assert rapport.files == []


# === 3. les cinq regles de refus, une par une ============================

AUJOURD_HUI = date(2026, 8, 26)


def decision(texte: str, **kw):
    contexte = DecisionContext(
        today=AUJOURD_HUI,
        allowed_vat_rates=(Decimal("0"), Decimal("7"), Decimal("10"), Decimal("20")),
        **kw,
    )
    return decide(extract_document([texte]), contexte)


def test_a_clean_invoice_still_passes_every_new_rule():
    """Le test negatif, et c'est le plus important du lot.

    Cinq regles de refus ajoutees d'un coup, c'est cinq facons de bloquer
    une comptabilite qui marchait. Celui-ci garantit qu'une facture
    ordinaire passe toujours.
    """
    verdict = decision(facture("FAC-ACH-2026-700"))
    assert verdict.action != ACTION_REVIEW


def test_an_invoice_dated_in_the_future_is_refused():
    verdict = decision(facture("FAC-ACH-2026-511", jour="15/01/2027"))
    assert verdict.action == ACTION_REVIEW
    assert any("futur" in motif for motif in verdict.reasons)


def test_a_vat_rate_outside_the_configured_list_is_refused():
    verdict = decision(
        facture("FAC-ACH-2026-512", taux="17", tva="1 062.50", ttc="7 312.50")
    )
    assert verdict.action == ACTION_REVIEW
    assert any("17" in motif and "TVA" in motif for motif in verdict.reasons)


def test_a_negative_invoice_that_is_not_a_credit_note_is_refused():
    verdict = decision(
        facture("FAC-ACH-2026-513", ht="-1 000.00", tva="-200.00", ttc="-1 200.00")
    )
    assert verdict.action == ACTION_REVIEW
    assert any("negatif" in motif for motif in verdict.reasons)


def test_a_supplier_invoice_without_a_usable_ice_is_refused():
    verdict = decision(
        facture("FAC-ACH-2026-515", fournisseur="SERVICES GENERAUX MAROC", ice="")
    )
    assert verdict.action == ACTION_REVIEW
    assert any("ICE exploitable" in motif for motif in verdict.reasons)


def test_a_vat_rate_is_not_judged_when_the_configuration_is_silent():
    """Une liste vide veut dire "non renseigne", pas "aucun taux permis".

    Confondre les deux refuserait toutes les factures d'un client qui n'a
    pas renseigne VAT_RATES_AVAILABLE.
    """
    contexte = DecisionContext(today=AUJOURD_HUI, allowed_vat_rates=None)
    verdict = decide(
        extract_document([facture("FAC-ACH-2026-516", taux="17",
                                  tva="1 062.50", ttc="7 312.50")]),
        contexte,
    )
    assert not any("taux de TVA" in motif for motif in verdict.reasons)


@pytest.mark.parametrize("nom", [
    "SARL", "SA", "SNC", "SOCIETE", "DIVERS", "INCONNU", "N/A", "SARL AU",
])
def test_a_generic_party_name_is_never_usable(nom):
    assert not usable_party_name(nom)


@pytest.mark.parametrize("nom", [
    "ATLAS BUREAU SARL", "GLOBAL TECH PARTS LTD", "NORTH DATA SARL",
    "SERVICES GENERAUX MAROC", "EURO INDUSTRIAL PARTS GMBH",
])
def test_a_real_company_name_is_still_usable(nom):
    assert usable_party_name(nom)
