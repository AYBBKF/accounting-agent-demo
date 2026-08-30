"""Le journal en partie double : chaque euro debite a son credit.

Ces tests verifient la comptabilite elle-meme : l'equilibre de chaque
type d'ecriture, l'inversion exacte des avoirs, le refus des
desequilibres avec l'ecart exact, l'interdiction d'inventer un compte,
l'idempotence par piece, et le recapitulatif TVA calcule sans
quarantaines ni doublons - par construction, puisqu'ils n'ont jamais
pose d'ecriture.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from app import ledger

D = Decimal


@pytest.fixture
def db_path():
    chemin = tempfile.mktemp(suffix=".db")
    ledger.ensure_schema(chemin)
    yield chemin
    Path(chemin).unlink(missing_ok=True)


def _mapping_complet():
    return ledger.AccountMapping({
        "banque": ["5141", "Banque"],
        "frais_bancaires": ["6147", "Services bancaires"],
    })


def _entry(kind, piece, **kw):
    base = dict(company_id="xblaste", kind=kind, piece=piece,
                entry_date="2026-08-15")
    base.update(kw)
    return ledger.build_entry(_mapping_complet(), **base)


def _equilibre(entry):
    debit = sum((l.debit for l in entry.lines), D("0"))
    credit = sum((l.credit for l in entry.lines), D("0"))
    return debit, credit


# --- equilibre par type d'operation ----------------------------------------


def test_la_facture_d_achat_est_equilibree(db_path):
    e = _entry("facture_achat", "F-1", ht=D("5000"), tva=D("1000"), ttc=D("6000"))
    debit, credit = _equilibre(e)
    assert debit == credit == D("6000")
    assert e.journal == "AC"
    comptes = {l.compte: l for l in e.lines}
    assert comptes["6111"].debit == D("5000")     # charge
    assert comptes["3455"].debit == D("1000")     # TVA deductible
    assert comptes["4411"].credit == D("6000")    # dette fournisseur


def test_la_facture_de_vente_est_equilibree(db_path):
    e = _entry("facture_vente", "V-1", ht=D("10000"), tva=D("2000"), ttc=D("12000"))
    debit, credit = _equilibre(e)
    assert debit == credit == D("12000")
    comptes = {l.compte: l for l in e.lines}
    assert comptes["3421"].debit == D("12000")    # creance client
    assert comptes["7111"].credit == D("10000")
    assert comptes["4455"].credit == D("2000")


def test_l_avoir_fournisseur_inverse_la_facture(db_path):
    facture = _entry("facture_achat", "F-2", ht=D("1000"), tva=D("200"), ttc=D("1200"))
    avoir = _entry("avoir_fournisseur", "AV-2", ht=D("1000"), tva=D("200"), ttc=D("1200"))
    par_compte_f = {l.compte: (l.debit, l.credit) for l in facture.lines}
    par_compte_a = {l.compte: (l.debit, l.credit) for l in avoir.lines}
    for compte, (deb, cred) in par_compte_f.items():
        assert par_compte_a[compte] == (cred, deb), (
            f"l'avoir doit inverser exactement le compte {compte}"
        )


def test_l_avoir_client_inverse_la_vente(db_path):
    vente = _entry("facture_vente", "V-3", ht=D("500"), tva=D("100"), ttc=D("600"))
    avoir = _entry("avoir_client", "AVC-3", ht=D("500"), tva=D("100"), ttc=D("600"))
    par_compte_v = {l.compte: (l.debit, l.credit) for l in vente.lines}
    par_compte_a = {l.compte: (l.debit, l.credit) for l in avoir.lines}
    for compte, (deb, cred) in par_compte_v.items():
        assert par_compte_a[compte] == (cred, deb)


def test_paiement_et_reglement_sont_equilibres(db_path):
    p = _entry("paiement_fournisseur", "PAY-1", montant=D("6000"))
    r = _entry("reglement_client", "ENC-1", montant=D("12000"))
    f = _entry("frais_bancaires", "FRAIS-1", montant=D("45.50"))
    for e in (p, r, f):
        debit, credit = _equilibre(e)
        assert debit == credit
        assert e.journal == "BQ"
    assert {l.compte for l in p.lines} == {"4411", "5141"}
    assert {l.compte for l in r.lines} == {"5141", "3421"}
    assert {l.compte for l in f.lines} == {"6147", "5141"}


def test_le_paiement_ne_recree_pas_la_facture(db_path):
    """Facture et paiement : DEUX evenements, deux pieces, aucun compte de
    charge ni de produit dans le paiement."""
    p = _entry("paiement_fournisseur", "PAY-F-1", montant=D("6000"))
    assert "6111" not in {l.compte for l in p.lines}
    assert "7111" not in {l.compte for l in p.lines}


# --- refus ------------------------------------------------------------------


def test_un_desequilibre_est_refuse_avec_l_ecart_exact(db_path):
    with pytest.raises(ledger.LedgerImbalance, match="450"):
        _entry("facture_achat", "F-KO", ht=D("5000"), tva=D("1000"), ttc=D("6450"))


def test_une_ecriture_desequilibree_ne_peut_pas_etre_enregistree(db_path):
    entry = ledger.Entry(
        company_id="xblaste", journal="OD", piece="OD-KO",
        entry_date="2026-08-15",
        lines=[ledger.Line("6111", "Achat", debit=D("100"))],
    )
    with pytest.raises(ledger.LedgerImbalance):
        ledger.record(db_path, entry)
    assert ledger.entries_for(db_path, "xblaste") == []


def test_un_compte_manquant_degrade_en_a_valider_sans_inventer(db_path):
    """Le template ne declare pas la banque : le paiement part A_VALIDER,
    motive, sans le moindre numero de compte invente."""
    sans_banque = ledger.AccountMapping()          # plan du template seul
    e = ledger.build_entry(
        sans_banque, company_id="xblaste", kind="paiement_fournisseur",
        piece="PAY-X", entry_date="2026-08-15", montant=D("100"),
    )
    assert e.statut == ledger.STATUT_A_VALIDER
    assert "banque" in e.motif
    assert e.lines == []                            # AUCUN compte pose
    assert ledger.sheet_rows(e) == [], "une A_VALIDER n'entre pas au classeur"
    assert ledger.record(db_path, e) is True        # mais elle est TRACEE
    lignes = ledger.entries_for(db_path, "xblaste", "PAY-X")
    assert lignes[0]["statut"] == "A_VALIDER"


def test_un_evenement_inconnu_est_refuse(db_path):
    with pytest.raises(ledger.LedgerError, match="inconnu"):
        _entry("pourboire", "X-1", montant=D("1"))


# --- idempotence et audit ---------------------------------------------------


def test_le_rejeu_de_la_meme_piece_n_ecrit_rien(db_path):
    e = _entry("facture_achat", "F-10", ht=D("100"), tva=D("20"), ttc=D("120"))
    assert ledger.record(db_path, e) is True
    for _ in range(3):
        assert ledger.record(db_path, e) is False
    assert len(ledger.entries_for(db_path, "xblaste", "F-10")) == 3  # 3 lignes, 1 piece


def test_la_meme_piece_coexiste_dans_deux_entreprises(db_path):
    e1 = _entry("facture_achat", "F-11", ht=D("100"), tva=D("20"), ttc=D("120"))
    e2 = ledger.build_entry(
        _mapping_complet(), company_id="v2-smoke", kind="facture_achat",
        piece="F-11", entry_date="2026-08-15",
        ht=D("100"), tva=D("20"), ttc=D("120"),
    )
    assert ledger.record(db_path, e1) is True
    assert ledger.record(db_path, e2) is True
    assert len(ledger.entries_for(db_path, "xblaste", "F-11")) == 3
    assert len(ledger.entries_for(db_path, "v2-smoke", "F-11")) == 3


def test_le_rapport_d_equilibre_rend_un_ecart_nul_partout(db_path):
    ledger.record(db_path, _entry("facture_achat", "F-20",
                                  ht=D("100"), tva=D("20"), ttc=D("120")))
    ledger.record(db_path, _entry("facture_vente", "V-20",
                                  ht=D("300"), tva=D("60"), ttc=D("360")))
    ledger.record(db_path, _entry("paiement_fournisseur", "PAY-20",
                                  montant=D("120")))
    rapport = ledger.balance_report(db_path, "xblaste")
    assert rapport, "le rapport ne doit pas etre vide"
    for piece, agregat in rapport.items():
        assert agregat["ecart"] == D("0"), f"{piece} desequilibree"


def test_l_extourne_annule_sans_effacer(db_path):
    original = _entry("facture_achat", "F-30", ht=D("100"), tva=D("20"), ttc=D("120"))
    ledger.record(db_path, original)
    contre = ledger.reversal_entry(original, motif="facture annulee par le fournisseur")
    assert ledger.record(db_path, contre) is True

    lignes = ledger.entries_for(db_path, "xblaste")
    assert any(l["piece"] == "F-30" for l in lignes), "l'originale reste"
    assert any(l["piece"] == "F-30-EXT" for l in lignes)
    total_debit = sum(D(l["debit"]) for l in lignes)
    total_credit = sum(D(l["credit"]) for l in lignes)
    assert total_debit == total_credit


# --- recapitulatif TVA ------------------------------------------------------


def test_le_recap_tva_est_exact_par_periode(db_path):
    ledger.record(db_path, ledger.build_entry(
        _mapping_complet(), company_id="xblaste", kind="facture_vente",
        piece="V-40", entry_date="2026-08-10",
        ht=D("1000"), tva=D("200"), ttc=D("1200")))
    ledger.record(db_path, ledger.build_entry(
        _mapping_complet(), company_id="xblaste", kind="facture_achat",
        piece="F-40", entry_date="2026-08-12",
        ht=D("500"), tva=D("50"), ttc=D("550")))
    ledger.record(db_path, ledger.build_entry(
        _mapping_complet(), company_id="xblaste", kind="facture_vente",
        piece="V-41", entry_date="2026-09-01",
        ht=D("100"), tva=D("20"), ttc=D("120")))

    recap = ledger.tva_recap(db_path, "xblaste")
    aout = next(r for r in recap if r["periode"] == "2026-08")
    assert aout["tva_collectee"] == "200"
    assert aout["tva_deductible"] == "50"
    assert aout["tva_due"] == "150"
    septembre = next(r for r in recap if r["periode"] == "2026-09")
    assert septembre["tva_due"] == "20"


def test_le_recap_ignore_les_a_valider(db_path):
    sans_banque = ledger.AccountMapping()
    ledger.record(db_path, ledger.build_entry(
        sans_banque, company_id="xblaste", kind="paiement_fournisseur",
        piece="PAY-KO", entry_date="2026-08-15", montant=D("100")))
    assert ledger.tva_recap(db_path, "xblaste") == []


def test_un_avoir_reduit_la_tva_de_la_periode(db_path):
    ledger.record(db_path, ledger.build_entry(
        _mapping_complet(), company_id="xblaste", kind="facture_vente",
        piece="V-50", entry_date="2026-08-10",
        ht=D("1000"), tva=D("200"), ttc=D("1200")))
    ledger.record(db_path, ledger.build_entry(
        _mapping_complet(), company_id="xblaste", kind="avoir_client",
        piece="AVC-50", entry_date="2026-08-20",
        ht=D("500"), tva=D("100"), ttc=D("600")))
    recap = ledger.tva_recap(db_path, "xblaste")
    assert recap[0]["tva_collectee"] == "100"


def test_le_recap_tva_est_isole_par_entreprise(db_path):
    ledger.record(db_path, ledger.build_entry(
        _mapping_complet(), company_id="xblaste", kind="facture_vente",
        piece="V-60", entry_date="2026-08-10",
        ht=D("1000"), tva=D("200"), ttc=D("1200")))
    assert ledger.tva_recap(db_path, "v2-smoke") == []


# --- projection classeur ----------------------------------------------------


def test_la_projection_suit_les_colonnes_du_template(db_path):
    e = _entry("facture_vente", "V-70", ht=D("1000"), tva=D("200"), ttc=D("1200"))
    lignes = ledger.sheet_rows(e)
    assert len(lignes) == 3
    assert lignes[0][:5] == ["2026-08-15", "VE", "V-70", "3421", "Client"]
    assert lignes[0][5] == "1200"          # debit client TTC
    assert lignes[1][3] == "7111" and lignes[1][6] == "1000"
    assert lignes[2][3] == "4455" and lignes[2][6] == "200"
