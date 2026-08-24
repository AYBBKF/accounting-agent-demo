"""Reparation ponctuelle des archives Drive deposees avant la correction.

Les pieces issues d'un ZIP ont ete archivees avec le contenu de l'ARCHIVE
PARENTE : douze fichiers portant douze noms de PDF, mais un seul et meme
contenu, celui du .zip. Le lien existait, la piece etait fausse.

Cette tache s'execute UNE FOIS au demarrage, dans le conteneur, avec les
identifiants Composio deja configures. Elle ne touche a AUCUNE ligne
comptable : elle ne fait que redeposer le bon contenu dans Drive, corriger
le lien dans la ligne existante de 14_IMPORTS_LOG, et mettre les faux
fichiers en quarantaine.

Deux garde-fous rendent la reprise sure :

  * un marqueur versionne dans la table `migrations` ;
  * un drapeau `archive_repaired` par document.

Un second lancement ne redepose rien et ne modifie rien.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import doc_store as store
from app import doc_vault as vault
from app.db import connect
from app.attachments import extract_member, is_zip, sha256_of
from app.doc_pipeline import REVIEW_DRIVE_FOLDER, drive_file_id, drive_link
from app.doc_routing import route_for

logger = logging.getLogger("demo_bot.drive_repair")

MIGRATION_KEY = "drive_archive_content"
MIGRATION_VERSION = 1
QUARANTINE_FOLDER = "ZZ_QUARANTAINE_ARCHIVES_ZIP"
PDF_MAGIC = b"%PDF-"

# --- nettoyage des enregistrements parasites du 23/08/2026 ----------------
# Une validation humaine en echec avait supprime la fiche de deux documents
# DEJA archives et DEJA journalises ; le cycle suivant les a recrees de
# zero, trois fois. Ces trois cles sont les enregistrements parasites, et
# eux seuls. Ils ne sont supprimes QUE si l'enregistrement canonique du
# meme document existe encore : sinon, ils SONT le document et on n'y
# touche pas.
CLEANUP_KEY = "cleanup_duplicate_records"
CLEANUP_VERSION = 1
DUPLICATE_KEY_PREFIXES = ("29dee48b317c", "4b6ea8b8a6e3", "af8348a31d13")

# --- rattachement des fiches vivantes a leur ligne d'origine --------------
# L'inspection a montre que les fiches canoniques cfa6d9b526eb et
# d5331e88d32b n'existent plus : ce sont les fiches recreees qui portent
# aujourd'hui l'etat, les boutons et l'archive. On les rattache donc a la
# ligne de journal et a l'archive Drive d'origine. DEUX champs seulement
# sont touches : `log_row` et `drive_link`. L'etat, l'empreinte, la
# reference stable, l'identifiant du message Telegram et tous les etats de
# notification restent intacts.
RESTORE_KEY = "restore_canonical_links"
RESTORE_VERSION = 1
# --- retour en validation des cinq documents du Pack V2 -------------------
# Cinq pieces avaient ete comptabilisees automatiquement alors que leur
# lecture etait ambigue : deux TTC possibles, une devise etrangere sans
# taux, un TTC absent, un avoir sans facture d'origine identifiable. Les
# regles corrigees les auraient toutes retenues. Leurs lignes comptables
# sont supprimees du classeur ; leur fiche revient donc en attente de
# decision, et redemande son arbitrage UNE seule fois.
# Version 2 : la premiere execution a bien remis quatre documents en
# attente, mais FAC-V2-AMB-002 est repartie en comptabilite au cycle
# suivant. Le document reel annonce "MONTANT A PAYER 2 600.00" a cote de
# "Total TTC 2 400.00", et ce libelle ne figurait pas dans la liste de
# synonymes. L'extraction reconnait desormais un total final a sa forme ;
# la tache rejoue donc pour ramener cette seule fiche en attente. Les
# quatre autres, deja en `needs_review`, sont laissees intactes : elles
# portent chacune UNE demande de validation en cours.
RESET_V2_KEY = "reset_pack_v2_documents"
RESET_V2_VERSION = 2
RESET_V2_NUMEROS = (
    "FAC-V2-AMB-001",
    "FAC-V2-AMB-002",
    "FAC-V2-INC-001",
    "FAC-V2-USD-001",
    "AV-V2-INC-001",
)

CANONICAL_LINKS = (
    {
        "prefix": "4b6ea8b8a6e3",
        "numero": "CI-2026-045",
        "log_row": 9,
        "drive_id": "1k4OhGRE-Plpr6IkQD5X95nVzR2NVxDBP",
    },
    {
        "prefix": "af8348a31d13",
        "numero": "EXP-2026-019",
        "log_row": 10,
        "drive_id": "17ahnRBx0Nnr4EYKkeN5rl4ZSbT7LSM2v",
    },
)


class RepairError(RuntimeError):
    """Echec d'une reparation. Jamais porteur de secret."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- etat propre a la migration ------------------------------------------
# Tout ce que la reparation ajoute a la base vit ici, et nulle part ailleurs :
# une correction ponctuelle ne doit pas alourdir le schema du pipeline.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations (
    key TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    detail TEXT,
    updated_at TEXT NOT NULL
);
"""
_COLUMNS = (("archive_repaired", "INTEGER DEFAULT 0"), ("old_drive_id", "TEXT"))


def ensure_schema(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        for column, kind in _COLUMNS:
            if column not in existing:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {column} {kind}")
        conn.commit()


def migration_state(db_path: str, key: str, version: int) -> str:
    """Etat d'une migration versionnee. Vide si elle n'a jamais tourne.

    Une version plus recente que celle enregistree repart de zero : c'est ce
    qui rend une correction rejouable sans toucher a la base a la main.
    """
    ensure_schema(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT version, state FROM migrations WHERE key = ?", (key,)
        ).fetchone()
    if row is None or int(row[0]) != int(version):
        return ""
    return str(row[1] or "")


def set_migration(db_path: str, key: str, version: int, state: str, detail: str = "") -> None:
    ensure_schema(db_path)
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO migrations (key, version, state, detail, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "version = excluded.version, state = excluded.state, "
            "detail = excluded.detail, updated_at = excluded.updated_at",
            (key, int(version), state, detail, _now()),
        )
        conn.commit()


def mark_repaired(db_path: str, doc_key: str, drive_link: str, old_id: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE documents SET drive_link = ?, archive_repaired = 1, "
            "old_drive_id = ?, updated_at = ? WHERE doc_key = ?",
            (drive_link, old_id, _now(), doc_key),
        )
        conn.commit()


def reset_repaired(db_path: str, doc_key: str) -> None:
    """Rend un document a reparer. Sert aux tests et a une reprise forcee."""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE documents SET archive_repaired = 0 WHERE doc_key = ?", (doc_key,)
        )
        conn.commit()


def list_zip_archives(db_path: str, chat_id: int) -> list[dict[str, Any]]:
    """Documents issus d'une archive ET deja deposes dans Drive.

    Ce sont exactement ceux dont le contenu archive peut etre celui du ZIP
    parent : la reparation ne regarde rien d'autre.
    """
    import sqlite3

    ensure_schema(db_path)
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM documents WHERE chat_id = ? "
            "AND member_path IS NOT NULL AND member_path != '' "
            "AND drive_link IS NOT NULL AND drive_link != '' "
            "ORDER BY created_at",
            (str(chat_id),),
        ).fetchall()
        return [dict(r) for r in rows]


# --- sauvegardes ----------------------------------------------------------

def backup(db_path: str) -> dict[str, str]:
    """Copie de la base et etat en lecture seule des documents concernes.

    Faits AVANT toute ecriture : si la migration devait mal tourner, l'etat
    d'origine reste consultable sur le volume.
    """
    root = Path(db_path).resolve().parent / "backups"
    root.mkdir(parents=True, exist_ok=True)
    stamp = _now().replace(":", "-")
    db_copy = root / f"demo-avant-{MIGRATION_KEY}-v{MIGRATION_VERSION}-{stamp}.db"
    try:
        import sqlite3

        source = sqlite3.connect(db_path)
        target = sqlite3.connect(str(db_copy))
        with target:
            source.backup(target)
        target.close()
        source.close()
    except Exception as exc:  # noqa: BLE001 - repli sur une copie simple
        logger.warning("Sauvegarde SQLite par API impossible (%s), copie brute", exc)
        shutil.copyfile(db_path, db_copy)
    return {"db": str(db_copy), "root": str(root)}


def snapshot(db_path: str, rows: list[dict[str, Any]], folder: str) -> str:
    """Etat en lecture seule des documents, avant toute modification."""
    keep = (
        "doc_key", "filename", "container", "parent_filename", "member_path",
        "doc_type", "numero", "state", "stable_id", "tab", "row_index",
        "drive_link", "log_row", "file_sha256", "calendar_event",
    )
    payload = [{k: row.get(k) for k in keep} for row in rows]
    path = Path(folder) / f"documents-avant-{MIGRATION_KEY}-v{MIGRATION_VERSION}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(path)


# --- contenu reel du document --------------------------------------------

def real_pdf(worker, row: dict[str, Any]) -> bytes:
    """Le VRAI PDF du document : coffre d'abord, ZIP parent ensuite.

    Le client ne renvoie rien : tout est reconstitue depuis le volume ou
    depuis l'email d'origine.
    """
    db = worker._db_path
    chat = worker._chat_id
    expected = str(row["file_sha256"])
    member_path = str(row.get("member_path") or "")

    content = vault.load(db, chat, row["doc_key"], expected)
    if content is None:
        parent, _url = worker.download_parent(row)
        if member_path:
            content = extract_member(parent, member_path)
        elif is_zip(parent):
            content = None
        else:
            content = parent
        if content is None:
            raise RepairError(
                f"membre '{member_path}' introuvable dans l'archive d'origine"
            )
        if sha256_of(content) != expected:
            raise RepairError(
                "le fichier retrouve ne correspond plus au document analyse "
                "(empreinte differente)"
            )
        vault.save(db, chat, row["doc_key"], content)

    if not content.startswith(PDF_MAGIC):
        raise RepairError("le contenu retrouve n'est pas un PDF")
    return content


# --- Drive ----------------------------------------------------------------

def folder_name_of(worker, file_id: str) -> str:
    """Nom du dossier qui contient un fichier. Sert a retrouver l'annee
    exacte utilisee lors de l'archivage initial, sans la deviner."""
    meta = worker.execute(
        "GOOGLEDRIVE_GET_FILE_METADATA", {"fileId": file_id, "fields": "id,parents"}
    )
    parents = [str(p) for p in (meta.get("parents") or []) if p]
    if not parents:
        return ""
    parent = worker.execute(
        "GOOGLEDRIVE_GET_FILE_METADATA", {"fileId": parents[0], "fields": "id,name"}
    )
    return str(parent.get("name") or "")


def target_folder(worker, row: dict[str, Any], year: str) -> tuple[str, str]:
    """Dossier definitif du document, selon ce qu'il est DEVENU.

    Une piece deja comptabilisee rejoint sa categorie ; une piece qui attend
    encore une decision reste dans 'A verifier'.
    """
    pipeline = worker.pipeline
    written = row.get("state") in store.STATES_AFTER_SHEET or row.get("state") == store.COMPLETED
    if written:
        category = route_for(str(row.get("doc_type") or "")).drive_folder
    else:
        category = REVIEW_DRIVE_FOLDER
    return pipeline.archive_folder(category, year), category


def upload_real_pdf(worker, name: str, content: bytes, folder_id: str) -> str:
    key = worker.upload(name=name, mimetype="application/pdf", content=content)
    if not key:
        raise RepairError("depot du contenu refuse")
    args: dict[str, Any] = {
        "file_to_upload": {
            "name": name, "mimetype": "application/pdf", "s3key": key,
        }
    }
    if folder_id:
        args["folder_to_upload_to"] = folder_id
    return drive_file_id(drive_link(worker.execute("GOOGLEDRIVE_UPLOAD_FILE", args)))


def fetch_stored_bytes(worker, file_id: str) -> bytes | None:
    """Relit le fichier REELLEMENT stocke dans Drive.

    Un code HTTP ne prouve rien : seul le contenu redescendu prouve que le
    bon document a ete depose.
    """
    try:
        data = worker.execute("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": file_id})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Relecture Drive de %s impossible: %s", file_id, exc)
        return None
    info = data.get("file") if isinstance(data.get("file"), dict) else data
    inline = (info or {}).get("content_b64")
    if inline:
        import base64

        return base64.b64decode(inline)
    url = ""
    for field in ("s3url", "url", "download_url", "uri"):
        value = (info or {}).get(field)
        if value:
            url = str(value)
            break
    if not url:
        return None
    try:
        import httpx

        response = httpx.get(url, timeout=60.0, follow_redirects=True)
        response.raise_for_status()
        return response.content
    except Exception as exc:  # noqa: BLE001 - l'URL signee n'est jamais journalisee
        logger.warning("Telechargement de controle impossible (%s)", type(exc).__name__)
        return None


def verify_uploaded(worker, file_id: str, name: str, content: bytes, folder_id: str) -> dict:
    """Controle du fichier REELLEMENT stocke. Leve si quoi que ce soit cloche."""
    meta = worker.execute(
        "GOOGLEDRIVE_GET_FILE_METADATA",
        {"fileId": file_id, "fields": "id,name,mimeType,size,parents,md5Checksum"},
    )
    stored_name = str(meta.get("name") or "")
    mime = str(meta.get("mimeType") or "")
    size = int(meta.get("size") or 0)
    parents = [str(p) for p in (meta.get("parents") or []) if p]
    digest = str(meta.get("md5Checksum") or "")

    if not stored_name.lower().endswith(".pdf"):
        raise RepairError(f"nom stocke inattendu: {stored_name}")
    if mime != "application/pdf":
        raise RepairError(f"MIME stocke inattendu: {mime}")
    if size != len(content):
        raise RepairError(f"taille stockee {size} != {len(content)}")
    if digest and digest != hashlib.md5(content).hexdigest():
        raise RepairError("empreinte MD5 stockee differente du PDF extrait")
    if folder_id and parents and folder_id not in parents:
        raise RepairError("le fichier n'est pas dans le dossier attendu")

    downloaded = fetch_stored_bytes(worker, file_id)
    checked = "metadonnees"
    if downloaded is not None:
        if not downloaded.startswith(PDF_MAGIC):
            raise RepairError("le contenu stocke ne commence pas par %PDF-")
        if sha256_of(downloaded) != sha256_of(content):
            raise RepairError("SHA-256 du contenu stocke different du PDF extrait")
        checked = "contenu relu"
    return {
        "name": stored_name, "mime": mime, "size": size,
        "md5": digest, "parents": parents, "checked": checked,
    }


# --- journal d'import -----------------------------------------------------

def update_log_link(worker, log_row: int, old_id: str, new_id: str) -> bool:
    """Remplace le lien Drive DANS la ligne existante. Aucune ligne creee."""
    if not log_row or not old_id or not new_id:
        return False
    cell = f"14_IMPORTS_LOG!F{log_row}"
    data = worker.execute(
        "GOOGLESHEETS_BATCH_GET",
        {
            "spreadsheet_id": worker._spreadsheet_id,
            "ranges": [cell],
            "valueRenderOption": "FORMATTED_VALUE",
        },
    )
    ranges = data.get("valueRanges") or []
    values = (ranges[0].get("values") or [[]]) if ranges else [[]]
    detail = str(values[0][0]) if values and values[0] else ""
    if old_id not in detail:
        logger.warning("Ligne %s du journal : ancien lien absent, rien n'est modifie", log_row)
        return False
    worker.execute(
        "GOOGLESHEETS_VALUES_UPDATE",
        {
            "spreadsheet_id": worker._spreadsheet_id,
            "range": cell,
            "value_input_option": "RAW",
            "values": [[detail.replace(old_id, new_id)]],
        },
    )
    return True


# --- Calendar (lecture seule) --------------------------------------------

def read_calendar_event(worker, event_id: str) -> dict:
    """Lecture SEULE d'un evenement. Aucune creation, aucune modification."""
    try:
        event = worker.execute(
            "GOOGLECALENDAR_EVENTS_GET", {"calendar_id": "primary", "event_id": event_id}
        )
    except Exception as exc:  # noqa: BLE001
        return {"found": False, "error": str(exc)[:200]}
    payload = event.get("event") if isinstance(event.get("event"), dict) else event
    start = payload.get("start") or {}
    return {
        "found": bool(payload.get("id")),
        "id": str(payload.get("id") or ""),
        "summary": str(payload.get("summary") or ""),
        "start": str(start.get("dateTime") or start.get("date") or ""),
        "timezone": str(start.get("timeZone") or ""),
        "status": str(payload.get("status") or ""),
        "calendar": str(payload.get("organizer", {}).get("email") or "primary"),
        "html_link": str(payload.get("htmlLink") or ""),
    }


# --- inventaire et nettoyage des doublons ---------------------------------

def inventory(db_path: str, chat_id: int) -> list[dict[str, Any]]:
    """Etat REEL de chaque fiche, tel qu'il sera journalise avant tout
    nettoyage. Aucune ecriture."""
    keep = (
        "doc_key", "filename", "numero", "doc_type", "state", "drive_link",
        "log_row", "file_sha256", "gmail_message_id", "created_at",
        "last_notified_state",
    )
    return [
        {k: row.get(k) for k in keep}
        for row in store.list_documents(db_path, chat_id)
    ]


def canonical_twin(rows: list[dict[str, Any]], row: dict[str, Any]) -> dict[str, Any] | None:
    """Fiche PLUS ANCIENNE decrivant exactement le meme document.

    Meme email, meme empreinte de fichier, autre cle. C'est la definition
    du doublon : sans jumeau plus ancien, la fiche examinee est le seul
    enregistrement du document et ne doit jamais etre supprimee.
    """
    # `rows` vient de `list_documents`, trie par date de creation puis par
    # ordre d'insertion : la fiche canonique est donc celle qui apparait
    # AVANT. Comparer les horodatages seuls ne suffit pas, deux fiches
    # creees dans la meme seconde porteraient la meme valeur.
    try:
        position = next(
            i for i, r in enumerate(rows) if r["doc_key"] == row["doc_key"]
        )
    except StopIteration:
        return None
    for other in rows[:position]:
        if other.get("gmail_message_id") != row.get("gmail_message_id"):
            continue
        if other.get("file_sha256") != row.get("file_sha256"):
            continue
        return other
    return None


def delete_record(db_path: str, doc_key: str) -> None:
    """Suppression ciblee d'UNE fiche parasite, apres sauvegarde."""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM documents WHERE doc_key = ?", (doc_key,))
        conn.commit()


def cleanup_duplicates(worker) -> dict[str, Any]:
    """Retire les fiches parasites nommement identifiees, et elles seules."""
    db = worker._db_path
    chat = worker._chat_id
    store.ensure_schema(db)
    ensure_schema(db)
    if migration_state(db, CLEANUP_KEY, CLEANUP_VERSION) == "done":
        return {"skipped": True, "reason": "deja executee"}

    rows = inventory(db, chat)
    saved = backup(db)
    listing = Path(saved["root"]) / f"documents-avant-{CLEANUP_KEY}-v{CLEANUP_VERSION}.json"
    listing.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    report: dict[str, Any] = {
        "backup": saved["db"], "inventaire": str(listing),
        "supprimes": [], "conserves": [], "absents": [],
    }
    for prefix in DUPLICATE_KEY_PREFIXES:
        cible = next(
            (r for r in rows if str(r["doc_key"]).startswith(prefix)), None
        )
        if cible is None:
            report["absents"].append(prefix)
            continue
        jumeau = canonical_twin(rows, cible)
        detail = {
            "cle": prefix,
            "numero": cible.get("numero"),
            "etat": cible.get("state"),
            "drive": drive_file_id(str(cible.get("drive_link") or "")),
            "log_row": cible.get("log_row"),
            "creee": cible.get("created_at"),
        }
        if jumeau is None:
            detail["motif"] = "seul enregistrement de ce document : conserve"
            report["conserves"].append(detail)
            continue
        detail["canonique"] = str(jumeau["doc_key"])[:12]
        delete_record(db, str(cible["doc_key"]))
        report["supprimes"].append(detail)

    set_migration(db, CLEANUP_KEY, CLEANUP_VERSION, "done", "")
    return report


def restore_canonical_links(worker) -> dict[str, Any]:
    """Rattache les fiches vivantes a leur ligne de journal et a leur
    archive d'origine. Ne touche QUE `log_row` et `drive_link`."""
    db = worker._db_path
    chat = worker._chat_id
    store.ensure_schema(db)
    ensure_schema(db)
    if migration_state(db, RESTORE_KEY, RESTORE_VERSION) == "done":
        return {"skipped": True, "reason": "deja executee"}

    rows = inventory(db, chat)
    saved = backup(db)
    report: dict[str, Any] = {"backup": saved["db"], "corrigees": [], "ignorees": []}

    for cible in CANONICAL_LINKS:
        fiche = next(
            (r for r in rows if str(r["doc_key"]).startswith(cible["prefix"])), None
        )
        if fiche is None:
            report["ignorees"].append(
                {"cle": cible["prefix"], "motif": "fiche absente de la base"}
            )
            continue
        if str(fiche.get("numero") or "") != cible["numero"]:
            report["ignorees"].append({
                "cle": cible["prefix"],
                "motif": f"numero inattendu {fiche.get('numero')!r}, attendu {cible['numero']!r}",
            })
            continue

        lien = f"https://drive.google.com/file/d/{cible['drive_id']}/view"
        avant = {
            "log_row": fiche.get("log_row"),
            "drive_link": fiche.get("drive_link"),
            "state": fiche.get("state"),
            "file_sha256": fiche.get("file_sha256"),
            "last_notified_state": fiche.get("last_notified_state"),
        }
        sql = (
            "UPDATE documents SET log_row = {row}, drive_link = '{lien}', "
            "updated_at = '{when}' WHERE doc_key = '{cle}'"
        ).format(
            row=cible["log_row"], lien=lien, when=_now(), cle=str(fiche["doc_key"])
        )
        with connect(db) as conn:
            conn.execute(
                "UPDATE documents SET log_row = ?, drive_link = ?, updated_at = ? "
                "WHERE doc_key = ?",
                (int(cible["log_row"]), lien, _now(), str(fiche["doc_key"])),
            )
            conn.commit()

        relu = store.get_document(db, str(fiche["doc_key"])) or {}
        apres = {
            "log_row": relu.get("log_row"),
            "drive_link": relu.get("drive_link"),
            "state": relu.get("state"),
            "file_sha256": relu.get("file_sha256"),
            "last_notified_state": relu.get("last_notified_state"),
            "telegram_message_id": relu.get("telegram_message_id"),
            "member_path": relu.get("member_path"),
        }
        conforme = (
            int(apres["log_row"] or 0) == int(cible["log_row"])
            and drive_file_id(str(apres["drive_link"] or "")) == cible["drive_id"]
            and apres["state"] == avant["state"]
            and apres["file_sha256"] == avant["file_sha256"]
            and apres["last_notified_state"] == avant["last_notified_state"]
        )
        report["corrigees"].append({
            "cle": cible["prefix"], "numero": cible["numero"],
            "sql": sql, "avant": avant, "apres": apres, "conforme": conforme,
        })

    set_migration(db, RESTORE_KEY, RESTORE_VERSION, "done", "")
    return report


def reset_pack_v2_documents(worker) -> dict[str, Any]:
    """Remet en attente de validation les cinq documents du Pack V2.

    Ecrit UNIQUEMENT l'etat et les champs de notification. L'empreinte du
    fichier, la reference Gmail, le lien Drive et l'horodatage de creation
    ne sont pas touches : la fiche reste le meme document, elle attend
    seulement une decision au lieu d'en avoir recu une a tort.
    """
    db = worker._db_path
    chat = worker._chat_id
    store.ensure_schema(db)
    ensure_schema(db)
    if migration_state(db, RESET_V2_KEY, RESET_V2_VERSION) == "done":
        return {"skipped": True, "reason": "deja executee"}

    rows = inventory(db, chat)
    saved = backup(db)
    report: dict[str, Any] = {
        "backup": saved["db"], "remises": [], "absentes": [], "inchangees": [],
    }
    attendus = {n.upper() for n in RESET_V2_NUMEROS}

    for numero in RESET_V2_NUMEROS:
        fiche = next(
            (r for r in rows if str(r.get("numero") or "").upper() == numero.upper()),
            None,
        )
        if fiche is None:
            report["absentes"].append(numero)
            continue
        cle = str(fiche["doc_key"])
        avant = {
            "state": fiche.get("state"),
            "file_sha256": fiche.get("file_sha256"),
            "drive_link": fiche.get("drive_link"),
            "last_notified_state": fiche.get("last_notified_state"),
        }
        if avant["state"] == store.NEEDS_REVIEW:
            report["inchangees"].append({"numero": numero, "cle": cle})
            continue
        with connect(db) as conn:
            conn.execute(
                "UPDATE documents SET state = ?, stable_id = '', tab = '', "
                "row_index = 0, lines_written = 0, review_archive = 1, "
                "last_notified_state = '', notified_at = NULL, "
                "validation_notification_sent_at = NULL, telegram_message_id = 0, "
                "updated_at = ? WHERE doc_key = ?",
                (store.NEEDS_REVIEW, _now(), cle),
            )
            conn.commit()
        relu = store.get_document(db, cle) or {}
        apres = {
            "state": relu.get("state"),
            "stable_id": relu.get("stable_id"),
            "row_index": relu.get("row_index"),
            "file_sha256": relu.get("file_sha256"),
            "drive_link": relu.get("drive_link"),
            "last_notified_state": relu.get("last_notified_state"),
            "validation_notification_sent_at": relu.get("validation_notification_sent_at"),
        }
        conforme = (
            apres["state"] == store.NEEDS_REVIEW
            and not (apres["stable_id"] or "")
            and int(apres["row_index"] or 0) == 0
            and apres["file_sha256"] == avant["file_sha256"]
            and apres["drive_link"] == avant["drive_link"]
            and not (apres["last_notified_state"] or "")
        )
        report["remises"].append({
            "numero": numero, "cle": cle,
            "avant": avant, "apres": apres, "conforme": conforme,
        })

    # Filet : aucune autre fiche que celles nommees ci-dessus.
    touchees = {e["numero"].upper() for e in report["remises"]}
    assert touchees <= attendus

    set_migration(db, RESET_V2_KEY, RESET_V2_VERSION, "done", "")
    return report


def run(worker) -> dict[str, Any]:
    """Repare les archives, puis met les faux fichiers en quarantaine."""
    db = worker._db_path
    chat = worker._chat_id
    store.ensure_schema(db)
    ensure_schema(db)

    # Nettoyage des fiches parasites : marqueur propre, execute une fois,
    # AVANT toute sortie anticipee de la migration des archives. Un echec
    # ici ne doit jamais empecher le cycle Gmail de tourner.
    try:
        menage = cleanup_duplicates(worker)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nettoyage des doublons impossible : %s", type(exc).__name__)
    else:
        if menage.get("skipped"):
            logger.info("Nettoyage des doublons : %s", menage.get("reason"))
        else:
            logger.info("Sauvegarde avant nettoyage : %s", menage["backup"])
            logger.info("Inventaire des fiches : %s", menage["inventaire"])
            for entree in menage["supprimes"]:
                logger.info(
                    "Fiche parasite supprimee %s (%s, etat=%s, drive=%s, "
                    "journal=%s, creee=%s) - canonique conservee : %s",
                    entree["cle"], entree["numero"], entree["etat"],
                    entree["drive"], entree["log_row"], entree["creee"],
                    entree["canonique"],
                )
            for entree in menage["conserves"]:
                logger.warning(
                    "Fiche %s NON supprimee (%s) : %s",
                    entree["cle"], entree["numero"], entree["motif"],
                )
            for prefixe in menage["absents"]:
                logger.info("Fiche %s : absente de la base, rien a supprimer", prefixe)
            logger.info(
                "Nettoyage des doublons termine : %d supprimee(s), %d conservee(s), "
                "%d absente(s)",
                len(menage["supprimes"]), len(menage["conserves"]),
                len(menage["absents"]),
            )

    # Rattachement des fiches vivantes a leur ligne et a leur archive
    # d'origine, sur decision explicite du client.
    try:
        remise = restore_canonical_links(worker)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rattachement canonique impossible : %s", type(exc).__name__)
    else:
        if remise.get("skipped"):
            logger.info("Rattachement canonique : %s", remise.get("reason"))
        else:
            logger.info("Sauvegarde avant rattachement : %s", remise["backup"])
            for entree in remise["corrigees"]:
                logger.info("SQL execute : %s", entree["sql"])
                logger.info(
                    "Fiche %s (%s) avant : log_row=%s drive=%s | apres : "
                    "log_row=%s drive=%s etat=%s sha=%s notifie=%s telegram=%s "
                    "member_path=%s | conforme=%s",
                    entree["cle"], entree["numero"],
                    entree["avant"]["log_row"],
                    drive_file_id(str(entree["avant"]["drive_link"] or "")),
                    entree["apres"]["log_row"],
                    drive_file_id(str(entree["apres"]["drive_link"] or "")),
                    entree["apres"]["state"],
                    str(entree["apres"]["file_sha256"] or "")[:16],
                    entree["apres"]["last_notified_state"],
                    entree["apres"]["telegram_message_id"],
                    entree["apres"]["member_path"],
                    entree["conforme"],
                )
            for entree in remise["ignorees"]:
                logger.warning(
                    "Fiche %s non rattachee : %s", entree["cle"], entree["motif"]
                )

    # Retour en validation des cinq documents du Pack V2, sur decision
    # explicite du client. Un echec ici ne bloque jamais le cycle Gmail.
    try:
        retour = reset_pack_v2_documents(worker)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Retour en validation Pack V2 impossible : %s", type(exc).__name__)
    else:
        if retour.get("skipped"):
            logger.info("Retour en validation Pack V2 : %s", retour.get("reason"))
        else:
            logger.info("Sauvegarde avant retour en validation : %s", retour["backup"])
            for entree in retour["remises"]:
                logger.info(
                    "Document %s (%s) remis en validation : etat %s -> %s, "
                    "stable_id vide=%s, ligne=%s, sha inchange=%s, drive inchange=%s, "
                    "notification remise a zero=%s | conforme=%s",
                    entree["numero"], entree["cle"],
                    entree["avant"]["state"], entree["apres"]["state"],
                    not (entree["apres"]["stable_id"] or ""),
                    entree["apres"]["row_index"],
                    entree["apres"]["file_sha256"] == entree["avant"]["file_sha256"],
                    entree["apres"]["drive_link"] == entree["avant"]["drive_link"],
                    not (entree["apres"]["last_notified_state"] or ""),
                    entree["conforme"],
                )
            for numero in retour["absentes"]:
                logger.info("Document %s absent de la base, rien a remettre", numero)
            for entree in retour["inchangees"]:
                logger.info(
                    "Document %s (%s) deja en attente de validation, inchange",
                    entree["numero"], entree["cle"],
                )

    state = migration_state(db, MIGRATION_KEY, MIGRATION_VERSION)
    if state == "done":
        return {"skipped": True, "reason": "deja executee"}

    rows = list_zip_archives(db, chat)
    if not rows:
        set_migration(db, MIGRATION_KEY, MIGRATION_VERSION, "done", "aucun document")
        return {"skipped": True, "reason": "aucun document concerne"}

    report: dict[str, Any] = {"repaired": [], "failed": [], "quarantined": []}
    if state != "verified":
        saved = backup(db)
        report["backup"] = saved["db"]
        report["snapshot"] = snapshot(db, rows, saved["root"])
        logger.info("Sauvegarde avant migration : %s", saved["db"])

        for row in rows:
            if row.get("archive_repaired"):
                continue
            try:
                report["repaired"].append(repair_one(worker, row))
            except Exception as exc:  # noqa: BLE001 - un document ne bloque pas les autres
                logger.warning(
                    "Reparation impossible (%s): %s", str(row["doc_key"])[:12], exc
                )
                report["failed"].append(
                    {"doc_key": str(row["doc_key"])[:12], "erreur": str(exc)[:200]}
                )

        remaining = [
            r for r in list_zip_archives(db, chat) if not r.get("archive_repaired")
        ]
        if remaining:
            logger.warning(
                "%d document(s) non repare(s) : la quarantaine attend le prochain "
                "demarrage, les anciens fichiers restent en place", len(remaining)
            )
            return report
        set_migration(db, MIGRATION_KEY, MIGRATION_VERSION, "verified", "")

    # Quarantaine SEULEMENT une fois les douze vrais PDF verifies.
    report["quarantined"] = quarantine_all(worker, list_zip_archives(db, chat))
    set_migration(db, MIGRATION_KEY, MIGRATION_VERSION, "done", "")
    logger.info(
        "Migration des archives terminee : %d repare(s), %d en quarantaine",
        len(report["repaired"]), len(report["quarantined"]),
    )
    return report


def repair_one(worker, row: dict[str, Any]) -> dict[str, Any]:
    old_id = drive_file_id(str(row.get("drive_link") or ""))
    content = real_pdf(worker, row)
    year = folder_name_of(worker, old_id) if old_id else ""
    if not year.isdigit():
        year = str(datetime.now(timezone.utc).year)
    folder_id, category = target_folder(worker, row, year)
    name = str(row.get("filename") or "document.pdf")

    new_id = upload_real_pdf(worker, name, content, folder_id)
    if not new_id:
        raise RepairError("aucun identifiant retourne par Drive")
    checks = verify_uploaded(worker, new_id, name, content, folder_id)

    new_link = f"https://drive.google.com/file/d/{new_id}/view"
    updated = update_log_link(worker, int(row.get("log_row") or 0), old_id, new_id)
    mark_repaired(worker._db_path, row["doc_key"], new_link, old_id)
    return {
        "doc_key": str(row["doc_key"])[:12],
        "fichier": name,
        "ancien_id": old_id,
        "nouveau_id": new_id,
        "dossier": f"{category}/{year}",
        "mime": checks["mime"],
        "taille": checks["size"],
        "sha256": sha256_of(content),
        "controle": checks["checked"],
        "journal_ligne": int(row.get("log_row") or 0) if updated else 0,
    }


def quarantine_all(worker, rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    root = worker.pipeline.ensure_folder(worker._drive_folder)
    bin_id = worker.pipeline.ensure_folder(QUARANTINE_FOLDER, root)
    moved: list[dict[str, str]] = []
    for row in rows:
        old_id = str(row.get("old_drive_id") or "")
        if not old_id or not bin_id:
            continue
        try:
            meta = worker.execute(
                "GOOGLEDRIVE_GET_FILE_METADATA", {"fileId": old_id, "fields": "id,parents"}
            )
            parents = [str(p) for p in (meta.get("parents") or []) if p]
            if parents == [bin_id]:
                moved.append({"id": old_id, "etat": "deja en quarantaine"})
                continue
            args: dict[str, Any] = {"file_id": old_id, "add_parents": bin_id}
            if parents:
                args["remove_parents"] = ",".join(parents)
            worker.execute("GOOGLEDRIVE_MOVE_FILE", args)
            moved.append({"id": old_id, "etat": "deplace"})
        except Exception as exc:  # noqa: BLE001 - la quarantaine ne bloque jamais
            logger.warning("Quarantaine impossible pour %s: %s", old_id, exc)
            moved.append({"id": old_id, "etat": f"echec: {str(exc)[:80]}"})
    return moved