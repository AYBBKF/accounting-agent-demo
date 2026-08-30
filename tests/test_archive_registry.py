"""Archivage Drive systematique : registre, arborescence, reprise.

L'archive sans registre est une promesse inverifiable. Ces tests
verifient le registre (une archive par contenu et par entreprise), la
nouvelle arborescence Entreprise/Annee/Mois/Categorie, la racine par
identifiant Drive (le bug constate en E2E), l'archivage du ZIP original,
et la reprise apres un echec Drive sans double ecriture.
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest

from app import archive_log
from app import doc_pipeline
from app.attachments import DocumentFile


@pytest.fixture
def db_path():
    chemin = tempfile.mktemp(suffix=".db")
    archive_log.ensure_schema(chemin)
    yield chemin
    Path(chemin).unlink(missing_ok=True)


# --- registre ---------------------------------------------------------------


def test_un_contenu_ne_s_archive_qu_une_fois_par_entreprise(db_path):
    premier = archive_log.remember(
        db_path, company_id="xblaste", sha256="a" * 64,
        original_name="facture.pdf", drive_link="lien-1",
    )
    second = archive_log.remember(
        db_path, company_id="xblaste", sha256="a" * 64,
        original_name="facture_renommee.pdf", drive_link="lien-2",
    )
    assert premier is True and second is False
    assert archive_log.known(db_path, "xblaste", "a" * 64)["drive_link"] == "lien-1"


def test_le_meme_contenu_s_archive_dans_chaque_entreprise(db_path):
    assert archive_log.remember(db_path, company_id="xblaste",
                                sha256="b" * 64, drive_link="lx") is True
    assert archive_log.remember(db_path, company_id="v2-smoke",
                                sha256="b" * 64, drive_link="ls") is True
    assert archive_log.known(db_path, "xblaste", "b" * 64)["drive_link"] == "lx"
    assert archive_log.known(db_path, "v2-smoke", "b" * 64)["drive_link"] == "ls"


def test_le_registre_porte_les_metadonnees_d_audit(db_path):
    archive_log.remember(
        db_path, company_id="xblaste", sha256="c" * 64,
        original_name="releve.pdf", mimetype="application/pdf",
        size_bytes=2593, gmail_message_id="msg-1", reference="REL-2026-08",
        statut="comptabilise", category="Banque",
        drive_file_id="fid-1", drive_link="lien",
    )
    fiche = archive_log.known(db_path, "xblaste", "c" * 64)
    assert fiche["mimetype"] == "application/pdf"
    assert fiche["size_bytes"] == 2593
    assert fiche["gmail_message_id"] == "msg-1"
    assert fiche["category"] == "Banque"
    assert fiche["archived_at"]


def test_une_archive_sans_entreprise_est_refusee(db_path):
    with pytest.raises(ValueError):
        archive_log.remember(db_path, company_id="", sha256="d" * 64)


# --- pipeline : arborescence et racine --------------------------------------


class FauxGateway:
    """Passerelle Drive qui note chaque appel, sans reseau."""

    def __init__(self, fail_uploads: int = 0) -> None:
        self.dossiers: dict[str, str] = {}
        self.uploads: list[dict] = []
        self._fail = fail_uploads
        self._suite = 0

    def execute(self, slug, args):
        if slug == "GOOGLEDRIVE_FIND_FOLDER":
            cle = f"{args.get('parent_folder_id','')}/{args['name_exact']}"
            fid = self.dossiers.get(cle)
            return {"files": [{"id": fid, "mimeType": "application/vnd.google-apps.folder"}]} if fid else {"files": []}
        if slug == "GOOGLEDRIVE_CREATE_FOLDER":
            self._suite += 1
            fid = f"dossier-{self._suite}"
            cle = f"{args.get('parent_id','')}/{args['name']}"
            self.dossiers[cle] = fid
            return {"id": fid, "mimeType": "application/vnd.google-apps.folder"}
        if slug == "GOOGLEDRIVE_UPLOAD_FILE":
            if self._fail > 0:
                self._fail -= 1
                raise RuntimeError("Drive indisponible")
            self.uploads.append(args)
            return {"id": f"fichier-{len(self.uploads)}",
                    "webViewLink": f"https://drive/f{len(self.uploads)}"}
        raise AssertionError(f"appel inattendu : {slug}")

    def upload(self, *, name, mimetype, content):
        return f"s3-{name}"


def _pipeline(db_path, gw, root="1AbCdEfGhIjKlMnOpQrStUvWx"):
    return doc_pipeline.DocumentPipeline(
        gateway=gw, db_path=db_path, chat_id=1, spreadsheet_id="s",
        drive_root=root, company_id="xblaste",
    )


def _fichier(nom="facture.pdf", contenu=b"%PDF-1.4 test"):
    return DocumentFile(filename=nom, content=contenu, source="attachment")


def test_une_racine_donnee_par_identifiant_est_utilisee_telle_quelle(db_path):
    """Regression E2E : l'identifiant etait cherche comme un NOM et un
    dossier errant naissait a la racine du Drive."""
    gw = FauxGateway()
    p = _pipeline(db_path, gw)
    cible = p.archive_folder("Achats", 2026, 8)
    assert cible
    noms_crees = [c.split("/")[-1] for c in gw.dossiers]
    assert "1AbCdEfGhIjKlMnOpQrStUvWx" not in noms_crees, (
        "l'identifiant ne doit jamais devenir un nom de dossier"
    )


def test_l_arborescence_est_annee_mois_categorie(db_path):
    gw = FauxGateway()
    p = _pipeline(db_path, gw)
    p.archive_folder("Achats", 2026, 8)
    chemins = set(gw.dossiers)
    assert any(c.endswith("/2026") for c in chemins)
    assert any(c.endswith("/08") for c in chemins)
    assert any(c.endswith("/Achats") for c in chemins)
    # Le mois est bien SOUS l'annee, la categorie SOUS le mois.
    annee_id = gw.dossiers[next(c for c in chemins if c.endswith("/2026"))]
    mois_cle = next(c for c in chemins if c.endswith("/08"))
    assert mois_cle.startswith(f"{annee_id}/")


def test_l_archive_est_enregistree_au_registre(db_path):
    gw = FauxGateway()
    p = _pipeline(db_path, gw)
    fichier = _fichier()
    lien = p.archive(fichier, "Achats", 2026, "", month=8,
                     gmail_message_id="msg-9", reference="F-9",
                     statut="comptabilise")
    assert lien
    fiche = archive_log.known(db_path, "xblaste", fichier.sha256)
    assert fiche is not None
    assert fiche["category"] == "Achats"
    assert fiche["gmail_message_id"] == "msg-9"


def test_le_meme_contenu_n_est_pas_redepose(db_path):
    gw = FauxGateway()
    p = _pipeline(db_path, gw)
    fichier = _fichier()
    lien1 = p.archive(fichier, "Achats", 2026, "", month=8)
    lien2 = p.archive(_fichier("autre_nom.pdf", fichier.content),
                      "Achats", 2026, "", month=8)
    assert lien1 == lien2
    assert len(gw.uploads) == 1, "un seul depot Drive pour les memes octets"


def test_la_reprise_apres_echec_drive_ne_double_rien(db_path):
    """Premier depot en panne : rien au registre, pas de lien. Le second
    passage reussit et n'archive qu'une fois."""
    gw = FauxGateway(fail_uploads=1)
    p = _pipeline(db_path, gw)
    fichier = _fichier()
    with pytest.raises(Exception):
        p.archive(fichier, "Achats", 2026, "", month=8)
    assert archive_log.known(db_path, "xblaste", fichier.sha256) is None

    lien = p.archive(fichier, "Achats", 2026, "", month=8)
    assert lien
    assert len(gw.uploads) == 1
    assert archive_log.known(db_path, "xblaste", fichier.sha256) is not None


def test_le_zip_original_est_archive_dans_emails_zip(db_path):
    gw = FauxGateway()
    p = _pipeline(db_path, gw)
    contenu = b"PK\\x03\\x04 zip factice"
    lien = p.archive_original_bundle("dossier.zip", contenu, "msg-77")
    assert lien
    import hashlib

    fiche = archive_log.known(db_path, "xblaste",
                              hashlib.sha256(contenu).hexdigest())
    assert fiche["category"] == "Emails_ZIP"
    assert fiche["statut"] == "zip-original"
    # Rejeu : aucun second depot.
    assert p.archive_original_bundle("dossier.zip", contenu, "msg-77") == lien
    assert len(gw.uploads) == 1


def test_les_archives_de_deux_entreprises_ne_se_voient_pas(db_path):
    gw = FauxGateway()
    px = _pipeline(db_path, gw)
    ps = doc_pipeline.DocumentPipeline(
        gateway=gw, db_path=db_path, chat_id=1, spreadsheet_id="s2",
        drive_root="1ZyXwVuTsRqPoNmLkJiHgFeDcB", company_id="v2-smoke",
    )
    fichier = _fichier()
    lien_x = px.archive(fichier, "Achats", 2026, "", month=8)
    lien_s = ps.archive(_fichier("copie.pdf", fichier.content),
                        "Achats", 2026, "", month=8)
    assert lien_x != lien_s
    assert len(gw.uploads) == 2, "une archive PAR entreprise pour ce contenu"
