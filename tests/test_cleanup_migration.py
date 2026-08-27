"""Le nettoyage : audite, reversible, et incapable de partir tout seul.

Ce module teste autant ce que le nettoyage FAIT que ce qu'il REFUSE de
faire. Le second point compte davantage : un nettoyage qui s'execute par
inadvertance, ou qui supprime au lieu d'annuler, cause plus de degats que
l'anomalie qu'il corrige.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app import cleanup_migration as nettoyage
from app import doc_store as store
from app.cleanup_migration import (
    STATUS_CANCELLED,
    STATUS_SUPERSEDED,
    TAB_CANCELLATIONS,
    CleanupAborted,
)
from app.db import init_db
from app.doc_pipeline import DocumentPipeline
from app.review_sheet import REVIEW_HEADERS, TAB_REVIEW
from workbook_fake import FakeWorkbook

CHAT = 999653395

ACHATS = [
    ["ID", "Date", "Numero facture", "ID Fournisseur", "Fournisseur",
     "Description", "Montant HT", "Taux TVA"],
    ["FA-2026-030", "2026-08-26", "FAC-ACH-2026-503", "FRS-016",
     "EXPRESS SERVICE MAROC", "Import email", 1000.0, 20.0],
    ["FA-2027-032", "2027-01-15", "FAC-ACH-2026-511", "FRS-013",
     "BETA SERVICES SARL", "Import email", 1000.0, 20.0],
    ["FA-2026-033", "2026-08-26", "FAC-ACH-2026-512", "FRS-013",
     "BETA SERVICES SARL", "Import email", 1000.0, 17.0],
    ["FA-2026-034", "2026-08-26", "FAC-ACH-2026-513", "FRS-013",
     "BETA SERVICES SARL", "Import email", -1000.0, 20.0],
]

IMPORTS = [
    ["Date/Heure sync", "Type", "ID stable", "Action"],
    ["2026-08-26T17:09:31+00:00", "Facture d'achat", "FA-2027-032", "Cree"],
    ["2026-08-26T17:10:00+00:00", "Facture d'achat", "FA-2026-033", "Cree"],
    ["2026-08-26T17:10:49+00:00", "Facture d'achat", "FA-2026-034", "Cree"],
    ["2026-08-26T17:07:37+00:00", "Facture d'achat", "canon-505", "A valider"],
    ["2026-08-26T17:16:41+00:00", "Facture d'achat", "doublon-505", "A valider"],
]


@pytest.fixture
def monde(tmp_path):
    chemin = str(tmp_path / "demo.db")
    init_db(chemin)
    store.ensure_schema(chemin)

    workbook = FakeWorkbook()
    workbook.tabs["05_FACTURES_ACHATS"] = [list(r) for r in ACHATS]
    workbook.tabs["14_IMPORTS_LOG"] = [list(r) for r in IMPORTS]
    workbook.tabs[TAB_REVIEW] = [list(REVIEW_HEADERS)]

    pipeline = DocumentPipeline(
        workbook, db_path=chemin, chat_id=CHAT, spreadsheet_id="sheet-test",
    )

    # Deux fiches pour UN document physique : le doublon du 26 aout.
    for cle, ligne in (("canon-505", 2), ("doublon-505", 3)):
        store.claim_document(
            chemin, cle, CHAT,
            gmail_message_id="zip" if cle.startswith("canon") else "separe",
            attachment_id=cle, file_sha256="a" * 64, filename=f"{cle}.pdf",
        )
        store.update_document(
            chemin, cle, state=store.NEEDS_REVIEW, review_row=ligne,
            doc_type="facture_achat", numero="FAC-ACH-2026-505",
        )
        workbook.tabs[TAB_REVIEW].append([cle[:12]] + [""] * 11)

    for cle, identifiant in (("ecr-511", "FA-2027-032"),
                             ("ecr-512", "FA-2026-033"),
                             ("ecr-513", "FA-2026-034")):
        store.claim_document(
            chemin, cle, CHAT, gmail_message_id="zip", attachment_id=cle,
            file_sha256=cle * 4, filename=f"{cle}.pdf",
        )
        store.update_document(
            chemin, cle, state=store.COMPLETED, stable_id=identifiant,
            tab="05_FACTURES_ACHATS",
        )
    return pipeline, chemin, workbook


# === 1. le plan ne touche a rien =========================================

def test_the_plan_changes_absolutely_nothing(monde):
    pipeline, chemin, workbook = monde
    avant = [list(r) for r in workbook.rows("05_FACTURES_ACHATS")]
    workbook.calls.clear()

    nettoyage.plan(pipeline, chemin, CHAT)

    assert workbook.rows("05_FACTURES_ACHATS") == avant
    assert not [s for s, _ in workbook.calls if "UPDATE" in s or "CLEAR" in s]


def test_the_plan_names_the_three_wrong_entries(monde):
    pipeline, chemin, _ = monde
    projet = nettoyage.plan(pipeline, chemin, CHAT)

    numeros = {a["numero"] for a in projet.cancellations}
    assert numeros == {
        "FAC-ACH-2026-511", "FAC-ACH-2026-512", "FAC-ACH-2026-513",
    }
    identifiants = {a["identifiant"] for a in projet.cancellations}
    assert identifiants == {"FA-2027-032", "FA-2026-033", "FA-2026-034"}


def test_the_plan_keeps_the_before_image_of_each_line(monde):
    """Sans image AVANT, une annulation ne serait pas reversible."""
    pipeline, chemin, _ = monde
    projet = nettoyage.plan(pipeline, chemin, CHAT)
    for annulation in projet.cancellations:
        assert annulation["avant"]
        assert annulation["motif"]


def test_the_plan_pairs_each_duplicate_with_its_canonical(monde):
    pipeline, chemin, _ = monde
    projet = nettoyage.plan(pipeline, chemin, CHAT)

    assert len(projet.duplicates) == 1
    doublon = projet.duplicates[0]
    assert doublon["doc_key"] == "doublon-505"
    assert doublon["canonique"] == "canon-505"


def test_a_missing_entry_is_a_warning_not_a_crash(monde):
    """Si une ligne a deja ete corrigee a la main, on le DIT."""
    pipeline, chemin, workbook = monde
    workbook.tabs["05_FACTURES_ACHATS"] = [
        r for r in workbook.tabs["05_FACTURES_ACHATS"]
        if "FAC-ACH-2026-512" not in r
    ]
    projet = nettoyage.plan(pipeline, chemin, CHAT)
    assert any("FAC-ACH-2026-512" in a for a in projet.warnings)
    assert len(projet.cancellations) == 2


# === 2. rien ne part tout seul ===========================================

def test_execute_refuses_without_an_explicit_confirmation(monde):
    pipeline, chemin, _ = monde
    with pytest.raises(CleanupAborted, match="confirmed"):
        nettoyage.execute(pipeline, chemin, CHAT)


def test_execute_stops_before_writing_if_the_backup_fails(monde, monkeypatch):
    """La regle qui prime sur toutes : pas de filet, pas de nettoyage."""
    pipeline, chemin, workbook = monde
    from app.db_backup import BackupError

    monkeypatch.setattr(
        nettoyage, "verified_backup",
        lambda db, label: (_ for _ in ()).throw(BackupError("integrity_check=malformed")),
    )
    avant = [list(r) for r in workbook.rows("05_FACTURES_ACHATS")]

    with pytest.raises(CleanupAborted, match="integrity_check"):
        nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    assert workbook.rows("05_FACTURES_ACHATS") == avant


def test_the_module_is_not_registered_as_a_startup_task():
    """Deployer l'image ne doit declencher aucun nettoyage."""
    import app.drive_repair as repair
    import app.mail_worker as worker

    assert "cleanup_migration" not in Path("app/drive_repair.py").read_text()
    assert "cleanup_migration" not in Path("app/mail_worker.py").read_text()
    assert not hasattr(repair, "run_cleanup")
    assert not hasattr(worker.MailWorker, "run_cleanup")


# === 3. ce que l'execution fait reellement ===============================

def test_the_wrong_amounts_leave_the_totals_but_the_line_stays(monde):
    pipeline, chemin, workbook = monde
    nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    lignes = workbook.rows("05_FACTURES_ACHATS")
    identifiants = [str(l[0]) for l in lignes]
    # Aucune ligne supprimee, aucun identifiant reattribue.
    assert identifiants == [
        "FA-2026-030", "FA-2027-032", "FA-2026-033", "FA-2026-034",
    ]

    annulee = [l for l in lignes if str(l[0]) == "FA-2027-032"][0]
    assert not [c for c in annulee[1:] if isinstance(c, (int, float))]
    assert any("ANNULEE APRES CONTROLE" in str(c) for c in annulee)


def test_the_untouched_entry_keeps_its_amounts(monde):
    """Une seule ligne fautive de trop annulee serait une regression grave."""
    pipeline, chemin, workbook = monde
    nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    intacte = [
        l for l in workbook.rows("05_FACTURES_ACHATS")
        if str(l[0]) == "FA-2026-030"
    ][0]
    assert 1000.0 in intacte


def test_the_before_image_is_written_to_the_cancellation_journal(monde):
    pipeline, chemin, workbook = monde
    nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    journal = workbook.rows(TAB_CANCELLATIONS)
    assert len(journal) == 3
    contenu = " ".join(str(c) for ligne in journal for c in ligne)
    assert "FA-2027-032" in contenu
    assert "1000" in contenu          # le montant d'origine est conserve


def test_the_import_log_is_marked_never_emptied(monde):
    pipeline, chemin, workbook = monde
    avant = len(workbook.rows("14_IMPORTS_LOG"))

    nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    lignes = workbook.rows("14_IMPORTS_LOG")
    assert len(lignes) == avant                     # aucune ligne retiree
    statuts = {str(l[2]): str(l[3]) for l in lignes}
    assert statuts["FA-2027-032"].startswith(STATUS_CANCELLED)
    assert statuts["doublon-505"].startswith(STATUS_SUPERSEDED)
    assert "canon-505" in statuts["doublon-505"]    # la canonique est nommee
    assert statuts["canon-505"] == "A valider"      # la canonique ne bouge pas


def test_the_secondary_record_is_detached_never_deleted(monde):
    pipeline, chemin, _ = monde
    nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    fiche = store.get_document(chemin, "doublon-505")
    assert fiche is not None                        # toujours en base
    assert fiche["superseded_by"] == "canon-505"
    assert fiche["state"] == store.SUPERSEDED
    assert store.get_document(chemin, "canon-505")["superseded_by"] in (None, "")


def test_the_cancelled_entries_go_back_to_needs_review(monde):
    pipeline, chemin, _ = monde
    nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    for cle in ("ecr-511", "ecr-512", "ecr-513"):
        assert store.get_document(chemin, cle)["state"] == store.NEEDS_REVIEW


# === 4. la reconstruction REELLE de 21_A_VERIFIER ========================
# Defaut remonte par la revue : execute() annoncait duplicates_detached=1
# et l'onglet gardait ses deux lignes. Marquer la base ne retire pas une
# ligne d'un classeur.

def test_the_secondary_row_really_leaves_the_sheet(monde):
    pipeline, chemin, workbook = monde
    assert len(workbook.rows(TAB_REVIEW)) == 2

    rapport = nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    assert rapport["duplicates_detached"] == 1
    assert rapport["review_rows_after"] == 1
    assert len(lignes_reelles(pipeline)) == 1


def test_the_surviving_row_is_the_canonical_one(monde):
    pipeline, chemin, workbook = monde
    nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    restante = lignes_reelles(pipeline)[0]
    assert str(restante[0]).startswith("canon-505"[:12])


def test_the_headers_survive_the_rebuild(monde):
    pipeline, chemin, workbook = monde
    nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)
    assert workbook.row(TAB_REVIEW, 1) == list(REVIEW_HEADERS)


def test_review_row_is_recomputed_after_the_lines_move(monde):
    """Un compactage perime toutes les positions memorisees.

    Sans recalcul, la fiche canonique garderait `review_row=2` alors que
    sa ligne a bouge - et le prochain cycle ecrirait par-dessus une autre
    ligne.
    """
    pipeline, chemin, workbook = monde
    nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    canonique = store.get_document(chemin, "canon-505")
    assert canonique["review_row"] == 2
    prefixe = str(workbook.row(TAB_REVIEW, canonique["review_row"])[0])
    assert canonique["doc_key"].startswith(prefixe)

    # La fiche secondaire ne revendique plus AUCUNE ligne.
    assert int(store.get_document(chemin, "doublon-505")["review_row"] or 0) == 0


def test_a_second_execute_changes_nothing(monde):
    """Strictement idempotent : meme lignes, memes montants, memes etats."""
    pipeline, chemin, workbook = monde
    nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    quarantaine = [list(r) for r in lignes_reelles(pipeline)]
    achats = [list(r) for r in workbook.rows("05_FACTURES_ACHATS")]
    journal = len(workbook.rows(TAB_CANCELLATIONS))

    second = nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    assert lignes_reelles(pipeline) == quarantaine
    assert workbook.rows("05_FACTURES_ACHATS") == achats
    assert second["duplicates_detached"] == 0     # plus rien a rattacher
    assert len(workbook.rows(TAB_CANCELLATIONS)) == journal


# === 5. a l'echelle de la production : 58 -> 47 ==========================

def lignes_reelles(pipeline) -> list[list]:
    """Les lignes de quarantaine telles qu'un lecteur les verra.

    On passe par la meme lecture que n'importe quel consommateur : l'etat
    interne de la grille n'interesse personne, ce qui compte est ce que le
    comptable voit.
    """
    relu = pipeline.read_tab(TAB_REVIEW)
    return [l for l in relu[1:] if any(str(c).strip() for c in l)]


def peupler(chemin: str, workbook, canoniques: int, doublons: int) -> None:
    """Reconstitue la forme exacte du classeur : N canoniques + M doublons."""
    workbook.tabs[TAB_REVIEW] = [list(REVIEW_HEADERS)]
    ligne = 2
    for index in range(canoniques):
        cle = f"canon-{index:03d}"
        store.claim_document(
            chemin, cle, CHAT, gmail_message_id="zip", attachment_id=cle,
            file_sha256=f"{index:064d}", filename=f"{cle}.pdf",
        )
        store.update_document(
            chemin, cle, state=store.NEEDS_REVIEW, review_row=ligne,
            doc_type="facture_achat", numero=f"FAC-{index:04d}",
        )
        workbook.tabs[TAB_REVIEW].append([cle[:12]] + [""] * 11)
        ligne += 1
    # Les doublons portent la MEME empreinte que leur canonique.
    for index in range(doublons):
        cle = f"dup-{index:03d}"
        store.claim_document(
            chemin, cle, CHAT, gmail_message_id="separe", attachment_id=cle,
            file_sha256=f"{index:064d}", filename=f"{cle}.pdf",
        )
        store.update_document(
            chemin, cle, state=store.NEEDS_REVIEW, review_row=ligne,
            doc_type="facture_achat", numero=f"FAC-{index:04d}",
        )
        workbook.tabs[TAB_REVIEW].append([cle[:12]] + [""] * 11)
        ligne += 1


def test_the_production_shape_goes_from_58_to_exactly_47(tmp_path):
    """La forme reelle du 26 aout : 58 lignes dont 11 doublons physiques."""
    chemin = str(tmp_path / "prod.db")
    init_db(chemin)
    store.ensure_schema(chemin)
    workbook = FakeWorkbook()
    workbook.tabs["05_FACTURES_ACHATS"] = [list(ACHATS[0])]
    workbook.tabs["14_IMPORTS_LOG"] = [list(IMPORTS[0])]
    pipeline = DocumentPipeline(
        workbook, db_path=chemin, chat_id=CHAT, spreadsheet_id="sheet-test",
    )
    peupler(chemin, workbook, canoniques=47, doublons=11)
    assert len(lignes_reelles(pipeline)) == 58

    rapport = nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    assert rapport["duplicates_detached"] == 11
    assert rapport["review_rows_after"] == 47
    assert len(lignes_reelles(pipeline)) == 47


def test_no_canonical_is_lost_and_no_gap_remains(tmp_path):
    chemin = str(tmp_path / "prod.db")
    init_db(chemin)
    store.ensure_schema(chemin)
    workbook = FakeWorkbook()
    pipeline = DocumentPipeline(
        workbook, db_path=chemin, chat_id=CHAT, spreadsheet_id="sheet-test",
    )
    peupler(chemin, workbook, canoniques=47, doublons=11)
    attendues = {f"canon-{i:03d}"[:12] for i in range(47)}

    nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    lignes = lignes_reelles(pipeline)
    presentes = {str(l[0]) for l in lignes}
    assert presentes == attendues                 # aucune canonique perdue
    assert all(str(l[0]).strip() for l in lignes)  # aucun trou residuel
    assert len(lignes) == len(presentes)           # aucune en double


def test_every_canonical_row_number_matches_its_record(tmp_path):
    chemin = str(tmp_path / "prod.db")
    init_db(chemin)
    store.ensure_schema(chemin)
    workbook = FakeWorkbook()
    pipeline = DocumentPipeline(
        workbook, db_path=chemin, chat_id=CHAT, spreadsheet_id="sheet-test",
    )
    peupler(chemin, workbook, canoniques=47, doublons=11)

    nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    for index in range(47):
        fiche = store.get_document(chemin, f"canon-{index:03d}")
        ligne = workbook.row(TAB_REVIEW, int(fiche["review_row"]))
        assert str(ligne[0]) == fiche["doc_key"][:12]


# === 6. le rollback, execute pour de vrai ================================

def test_every_modified_tab_is_backed_up_before_any_write(monde):
    pipeline, chemin, _ = monde
    rapport = nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    protegees = set(rapport["backups"])
    assert {"05_FACTURES_ACHATS", "14_IMPORTS_LOG", TAB_REVIEW} <= protegees


def test_the_rollback_really_restores_every_tab(monde):
    """Le rollback est APPELE, pas seulement decrit.

    C'est la seule facon de savoir qu'il fonctionne : une procedure de
    restauration jamais executee est une hypothese.
    """
    pipeline, chemin, workbook = monde
    workbook.tabs["16_LIGNES_FACTURES"] = [
        ["ID", "Designation", "Montant"],
        ["FA-2027-032", "Prestation", 1000.0],
        ["FA-2026-030", "Fournitures", 500.0],
    ]
    def contenu(onglet: str) -> list[list]:
        """Contenu SIGNIFIANT d'un onglet, sans les cellules vides de fin.

        Comparer les grilles brutes ferait echouer la comparaison sur des
        cellules vides que l'API ne rend meme pas. Ce qu'on veut savoir est
        si le comptable retrouve son onglet, pas si la grille a la meme
        largeur interne.
        """
        lignes = []
        for ligne in workbook.tabs.get(onglet, []):
            copie = list(ligne)
            while copie and str(copie[-1]) == "":
                copie.pop()
            lignes.append(copie)
        return lignes

    avant = {
        onglet: contenu(onglet)
        for onglet in ("05_FACTURES_ACHATS", "14_IMPORTS_LOG",
                       "16_LIGNES_FACTURES", TAB_REVIEW)
    }

    rapport = nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)
    assert workbook.tabs["05_FACTURES_ACHATS"] != avant["05_FACTURES_ACHATS"]
    assert workbook.tabs["16_LIGNES_FACTURES"] != avant["16_LIGNES_FACTURES"]

    nettoyage.rollback(pipeline, rapport["backups"])

    for onglet, attendu in avant.items():
        assert contenu(onglet) == attendu, f"{onglet} mal restaure"


def test_the_rollback_brings_back_the_amounts_of_the_detail_lines(monde):
    """Cas precis exige par la revue : 16_LIGNES_FACTURES."""
    pipeline, chemin, workbook = monde
    workbook.tabs["16_LIGNES_FACTURES"] = [
        ["ID", "Designation", "Montant"],
        ["FA-2026-033", "Prestation comptable", 1000.0],
    ]

    rapport = nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)
    apres = workbook.tabs["16_LIGNES_FACTURES"][1]
    assert 1000.0 not in apres                     # bien neutralisee

    nettoyage.rollback(pipeline, rapport["backups"])
    assert workbook.tabs["16_LIGNES_FACTURES"][1] == [
        "FA-2026-033", "Prestation comptable", 1000.0,
    ]


def test_the_rollback_brings_back_the_import_log_statuses(monde):
    """Cas precis exige par la revue : 14_IMPORTS_LOG."""
    pipeline, chemin, workbook = monde

    rapport = nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)
    marques = {str(l[2]): str(l[3]) for l in workbook.rows("14_IMPORTS_LOG")}
    assert marques["FA-2027-032"].startswith(STATUS_CANCELLED)

    nettoyage.rollback(pipeline, rapport["backups"])
    restaures = {str(l[2]): str(l[3]) for l in workbook.rows("14_IMPORTS_LOG")}
    assert restaures["FA-2027-032"] == "Cree"
    assert restaures["doublon-505"] == "A valider"


def test_a_tab_copy_that_does_not_match_stops_everything(monde, monkeypatch):
    """Une copie non conforme doit interrompre AVANT toute ecriture."""
    pipeline, chemin, workbook = monde
    avant = [list(r) for r in workbook.rows("05_FACTURES_ACHATS")]

    from app.doc_pipeline import PipelineError

    monkeypatch.setattr(
        pipeline, "backup_tab",
        lambda tab: (_ for _ in ()).throw(PipelineError(f"copie {tab} incomplete")),
    )
    with pytest.raises(CleanupAborted, match="copie d'onglet non verifiee"):
        nettoyage.execute(pipeline, chemin, CHAT, confirmed=True)

    assert workbook.rows("05_FACTURES_ACHATS") == avant
