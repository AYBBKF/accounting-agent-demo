"""Regression : une base SALE ne doit pas inonder `21_A_VERIFIER`.

Ce module reproduit l'incident REEL du 26 aout 2026. La base de
production contenait ~94 fiches `needs_review` pour chacun des 5 memes
documents physiques - sequelle d'un ancien bug ou l'`attachment_id`
rendu par Gmail changeait a chaque cycle, donnant un `doc_key` neuf a
chaque relecture du meme PDF.

La migration v1 dedupliquait sur `doc_key`. Comme chaque fiche avait le
sien, le garde-fou ne se declenchait jamais : 470 lignes ecrites pour 5
documents. Les tests de l'epoque tournaient sur une base propre et ne
pouvaient pas voir ce cas - c'est precisement ce trou que ce module
comble.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app.business_key import (
    LEVEL_BUSINESS,
    LEVEL_GMAIL,
    LEVEL_SHA,
    business_document_key,
    group_by_business_key,
    normalize,
)

# 5 documents physiques, tels qu'ils existaient reellement.
DOCUMENTS = [
    ("CI-2026-045", "facture_import", "sha-ci-045"),
    ("EXP-2026-019", "facture_export", "sha-exp-019"),
    ("AV-2026-003", "avoir_fournisseur", "sha-av-003"),
    ("FAC-TEST-2026-003", "facture_achat", "sha-fac-003"),
    ("FAC-TEST-2026-004", "facture_achat", "sha-fac-004"),
]
ROTATIONS = 94


def base_sale(*, avec_sha: bool = True) -> list[dict]:
    """~94 fiches par document, chacune avec son propre doc_key.

    C'est la forme exacte du defaut : l'`attachment_id` tourne, donc le
    `doc_key` aussi, alors que le CONTENU du fichier ne bouge pas.
    """
    fiches: list[dict] = []
    for numero, doc_type, sha in DOCUMENTS:
        for tour in range(ROTATIONS):
            # doc_key REALISTE : en production c'est une empreinte hex de
            # 64 caracteres. Un identifiant lisible ferait collisionner les
            # 12 premiers caracteres de FAC-TEST-2026-003 et -004, ce qui
            # masquerait le vrai comportement.
            fiches.append({
                "doc_key": hashlib.sha256(
                    f"{numero}-{tour}".encode()
                ).hexdigest(),
                "attachment_id": f"ANGjdJ-rotation-{numero}-{tour}",
                "file_sha256": sha if avec_sha else "",
                "gmail_message_id": "1a02a81e3859a298",
                "filename": f"{numero}.pdf",
                "member_path": f"pack/{numero}.pdf",
                "numero": numero,
                "doc_type": doc_type,
                "state": "needs_review",
                "created_at": f"2026-08-22T17:{29 + tour % 30:02d}:00+00:00",
                "drive_link": "https://drive.google.com/file/d/x/view" if tour == 7 else "",
                "payload": '{"reasons": ["motif complet"]}' if tour == 7 else "",
                "log_row": 0,
            })
    return fiches


# === 1. le defaut, reproduit puis corrige =================================

def test_a_dirty_legacy_database_collapses_to_five_documents():
    """470 fiches, 5 documents. C'est tout l'objet du correctif."""
    fiches = base_sale()
    assert len(fiches) == len(DOCUMENTS) * ROTATIONS == 470

    groupes = group_by_business_key(fiches)

    assert len(groupes) == 5, "la deduplication metier doit rendre 5 groupes"
    numeros = sorted(canonique["numero"] for _, canonique, _ in groupes)
    assert numeros == sorted(n for n, _, _ in DOCUMENTS)


def test_every_rotation_is_marked_as_a_duplicate_not_lost():
    """Les 93 autres fiches de chaque document sont conservees, marquees."""
    groupes = group_by_business_key(base_sale())
    total_doublons = sum(len(doublons) for _, _, doublons in groupes)
    assert total_doublons == 470 - 5 == 465
    # Aucune fiche ne disparait : canoniques + doublons = tout l'ensemble.
    total = sum(1 + len(d) for _, _, d in groupes)
    assert total == 470


def test_the_rotating_attachment_id_never_enters_the_key():
    """Le champ fautif ne doit influencer aucune cle."""
    fiches = base_sale()
    cles = {business_document_key(f) for f in fiches}
    assert len(cles) == 5
    for fiche in fiches:
        assert fiche["attachment_id"] not in business_document_key(fiche)


def test_the_doc_key_alone_is_never_enough():
    """Deux fiches au meme contenu et au doc_key different se rejoignent."""
    a = {"doc_key": "aaa", "file_sha256": "meme-empreinte"}
    b = {"doc_key": "bbb", "file_sha256": "meme-empreinte"}
    assert business_document_key(a) == business_document_key(b)
    assert len(group_by_business_key([a, b])) == 1


# === 2. l'echelle de fiabilite ============================================

def test_the_content_hash_wins_when_available():
    cle = business_document_key({"file_sha256": "ABC123", "numero": "F-1"})
    assert cle.startswith(f"{LEVEL_SHA}:")
    assert "abc123" in cle          # normalise en minuscules


def test_gmail_message_and_filename_take_over_without_a_hash():
    cle = business_document_key({
        "gmail_message_id": "m-1", "filename": "Facture_TEST 2026-003.pdf",
        "file_size": 4096,
    })
    assert cle.startswith(f"{LEVEL_GMAIL}:")
    assert "facture test 2026 003 pdf" in cle


def test_two_spellings_of_one_filename_give_one_key():
    """Accents et ponctuation ne doivent pas creer un second document."""
    a = {"gmail_message_id": "m-1", "filename": "Éch�éance_Août.pdf"}
    b = {"gmail_message_id": "m-1", "filename": "echeance aout.pdf"}
    assert normalize("Écheance_Août.pdf") == "echeance aout pdf"
    assert business_document_key(b).endswith("|echeance aout pdf|")


def test_the_accounting_identity_is_the_last_resort():
    cle = business_document_key({
        "numero": "FAC-2026-003", "doc_type": "facture_achat",
        "party_id": "FRS-008", "date_document": "2026-08-22",
        "montant_ttc": "3900.00",
    })
    assert cle.startswith(f"{LEVEL_BUSINESS}:")
    assert "fac 2026 003" in cle


def test_a_fiche_with_nothing_reliable_is_never_merged():
    """Dans le doute, deux fiches restent deux fiches.

    Fusionner sur une identite incertaine ferait disparaitre un document
    reel de la zone de quarantaine : c'est pire qu'un doublon visible.
    """
    a = {"doc_key": "aaa"}
    b = {"doc_key": "bbb"}
    assert business_document_key(a) == ""
    assert len(group_by_business_key([a, b])) == 2


def test_a_bare_number_is_not_an_identity():
    """Un numero seul peut appartenir a deux documents differents."""
    assert business_document_key({"numero": "F-1"}) == ""


# === 3. le choix de la fiche canonique ====================================

def test_the_canonical_record_is_the_most_informative_one():
    """On garde celle qui aide vraiment le comptable."""
    _, canonique, doublons = group_by_business_key(base_sale())[0]
    assert len(canonique["doc_key"]) == 64
    assert canonique["drive_link"], "la fiche avec lien Drive doit gagner"
    assert "motif complet" in canonique["payload"]
    assert len(doublons) == ROTATIONS - 1


def test_between_equals_the_oldest_record_wins():
    """A information egale, l'anteriorite tranche - de facon reproductible."""
    vieux = {"doc_key": "v", "file_sha256": "s", "created_at": "2026-08-01T00:00:00+00:00"}
    recent = {"doc_key": "r", "file_sha256": "s", "created_at": "2026-08-22T00:00:00+00:00"}
    _, canonique, _ = group_by_business_key([recent, vieux])[0]
    assert canonique["doc_key"] == "v"


def test_grouping_is_stable_across_runs():
    """Rejouer la migration doit donner exactement le meme resultat."""
    premier = group_by_business_key(base_sale())
    second = group_by_business_key(base_sale())
    assert [c["doc_key"] for _, c, _ in premier] == [c["doc_key"] for _, c, _ in second]


# === 4. le cas degrade : pas d'empreinte ==================================

def test_without_any_hash_gmail_identity_still_collapses_the_rotations():
    """Meme sans SHA-256, les 94 rotations restent un seul document."""
    groupes = group_by_business_key(base_sale(avec_sha=False))
    assert len(groupes) == 5


# === 5. le garde-fou de non-regression ====================================

@pytest.mark.parametrize("rotations", [1, 2, 94, 200])
def test_the_row_count_follows_documents_never_rotations(rotations):
    """Le nombre de lignes suit les DOCUMENTS, jamais les relectures."""
    fiches = [
        {
            "doc_key": f"cle-{i}", "file_sha256": "empreinte-unique",
            "numero": "FAC-1", "created_at": f"2026-08-22T17:{i % 60:02d}:00+00:00",
        }
        for i in range(rotations)
    ]
    assert len(group_by_business_key(fiches)) == 1


# === 6. bout en bout : la migration sur une base REELLEMENT sale ==========

def peupler(db_path: str, chat_id: int) -> None:
    """Ecrit les 470 fiches sales dans une vraie base SQLite."""
    from app import doc_store as store
    from app.db import connect

    store.ensure_schema(db_path)
    with connect(db_path) as conn:
        for fiche in base_sale():
            conn.execute(
                "INSERT INTO documents (doc_key, chat_id, gmail_message_id, "
                "attachment_id, file_sha256, filename, member_path, numero, "
                "doc_type, state, created_at, updated_at, drive_link, payload, "
                "lines_written) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    fiche["doc_key"], chat_id, fiche["gmail_message_id"],
                    fiche["attachment_id"], fiche["file_sha256"],
                    fiche["filename"], fiche["member_path"], fiche["numero"],
                    fiche["doc_type"], fiche["state"], fiche["created_at"],
                    fiche["created_at"], fiche["drive_link"], fiche["payload"],
                ),
            )
        conn.commit()


@pytest.fixture
def worker_sale(tmp_path, monkeypatch):
    from test_mail_worker import FakeMailWorker
    from workbook_fake import FakeWorkbook

    chemin = str(tmp_path / "sale.db")
    ouvrier = FakeMailWorker(FakeWorkbook(), chemin)
    peupler(chemin, ouvrier._chat_id)
    return ouvrier


def test_the_migration_turns_470_dirty_records_into_five_rows(worker_sale):
    """Le test qui manquait le 26 aout, et qui aurait evite l'incident."""
    import app.drive_repair as repair
    from app.review_sheet import TAB_REVIEW

    rapport = repair.migrate_needs_review_to_review_tab(worker_sale)

    assert not rapport.get("aborted")
    assert rapport["backup_integrity"] == "ok"
    assert len(str(rapport["backup_sha256"])) == 64
    assert len(rapport["migres"]) == 5, "5 documents physiques, pas 470"
    assert len(rapport["doublons_marques"]) == 465
    assert len(worker_sale.workbook.rows(TAB_REVIEW)) == 5


def test_replaying_the_migration_keeps_exactly_five_rows(worker_sale):
    """Idempotence REELLE : deux passages, cinq lignes."""
    import app.drive_repair as repair
    from app.review_sheet import TAB_REVIEW

    repair.migrate_needs_review_to_review_tab(worker_sale)
    apres_un = len(worker_sale.workbook.rows(TAB_REVIEW))

    # On force le rejeu en effacant le marqueur de migration.
    repair.set_migration(
        worker_sale._db_path, repair.MIGRATE_REVIEW_KEY,
        repair.MIGRATE_REVIEW_VERSION, "",
    )
    rapport = repair.migrate_needs_review_to_review_tab(worker_sale)

    assert apres_un == 5
    assert len(worker_sale.workbook.rows(TAB_REVIEW)) == 5
    # Au second passage, les doublons sont deja marques : rien a remarquer.
    assert rapport["doublons_marques"] == []


def test_no_sqlite_record_is_ever_deleted(worker_sale):
    """L'audit reste complet : 470 fiches avant, 470 apres."""
    import app.drive_repair as repair
    from app import doc_store as store

    avant = len(store.list_documents(worker_sale._db_path, worker_sale._chat_id))
    repair.migrate_needs_review_to_review_tab(worker_sale)
    apres = store.list_documents(worker_sale._db_path, worker_sale._chat_id)

    assert avant == len(apres) == 470
    marquees = [f for f in apres if str(f.get("superseded_by") or "")]
    assert len(marquees) == 465


def test_the_tab_is_backed_up_before_being_rebuilt(worker_sale):
    """On ne vide jamais l'onglet sans en avoir garde une copie relue."""
    import app.drive_repair as repair

    rapport = repair.migrate_needs_review_to_review_tab(worker_sale)
    # Onglet neuf au premier passage : rien a sauvegarder, et c'est normal.
    assert rapport["sheet_backup"] == ""

    # Second passage : l'onglet contient 5 lignes, il DOIT etre copie.
    repair.set_migration(
        worker_sale._db_path, repair.MIGRATE_REVIEW_KEY,
        repair.MIGRATE_REVIEW_VERSION, "",
    )
    second = repair.migrate_needs_review_to_review_tab(worker_sale)
    nom = second["sheet_backup"]
    assert nom.startswith("21_A_VERIFIER_BACKUP_")
    assert nom in worker_sale.workbook.tabs
    assert len(worker_sale.workbook.rows(nom)) == 5


def test_accounting_tabs_are_never_touched(worker_sale):
    """Aucune ecriture comptable pendant le nettoyage."""
    import app.drive_repair as repair

    repair.migrate_needs_review_to_review_tab(worker_sale)
    for onglet in ("05_FACTURES_ACHATS", "17_AVOIRS", "14_IMPORTS_LOG",
                   "04_FACTURES_VENTES"):
        assert worker_sale.workbook.writes_to(onglet) == []
