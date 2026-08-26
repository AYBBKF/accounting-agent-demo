"""Sauvegarde SQLite VERIFIEE, faite depuis le processus lui-meme.

Aucun shell, aucun outil externe : `sqlite3.Connection.backup()` produit
une copie coherente meme si des ecritures ont lieu pendant la copie -
ce qu'un `cp` ne garantit pas.

La raison d'etre de ce module tient en une phrase : une sauvegarde dont
on n'a pas verifie qu'elle est lisible n'est pas une sauvegarde, c'est
une croyance. On controle donc, dans l'ordre :

  1. le fichier existe ;
  2. sa taille est non nulle ;
  3. il s'ouvre comme une base SQLite ;
  4. `PRAGMA integrity_check` repond exactement `ok` ;
  5. son SHA-256 est calculable et journalise.

Si un seul controle echoue, on leve. L'appelant DOIT alors s'arreter
avant la moindre ecriture : mieux vaut une migration non faite qu'une
migration sans filet.

Rien de sensible n'est journalise : un chemin, une taille, une
empreinte. Aucune donnee metier, aucun secret.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("demo_bot.db_backup")

# Lu par blocs : une base de plusieurs dizaines de Mo ne doit pas etre
# chargee entierement en memoire dans un conteneur limite a 256 Mo.
_CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    """Sauvegarde absente, illisible ou corrompue. Jamais ignorable."""


def _stamp() -> str:
    """Horodatage UTC a la microseconde.

    La seconde ne suffit pas : deux sauvegardes prises dans la meme
    seconde porteraient le meme nom, et la seconde ecraserait la
    premiere - exactement ce qu'une sauvegarde ne doit jamais faire.
    """
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace(":", "-")
        .replace(".", "-")
    )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def integrity_ok(path: Path) -> tuple[bool, str]:
    """`PRAGMA integrity_check` sur la COPIE, pas sur l'original.

    C'est bien la copie qu'on s'apprete a considerer comme un filet :
    c'est elle qui doit etre saine.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    verdict = (rows[0][0] if rows and rows[0] else "").strip().lower()
    return verdict == "ok", verdict


def verify_backup_file(copie: Path) -> dict[str, object]:
    """Controles 2 a 5 sur un fichier de sauvegarde deja ecrit.

    Extrait de `verified_backup` pour etre testable seul, avec de vrais
    fichiers degrades - un filet dont on ne teste que le cas favorable
    ne prouve rien.
    """
    if not copie.exists():
        raise BackupError(f"sauvegarde absente apres copie : {copie}")

    taille = copie.stat().st_size
    if taille <= 0:
        raise BackupError(f"sauvegarde vide : {copie}")

    try:
        saine, verdict = integrity_ok(copie)
    except sqlite3.Error as exc:
        raise BackupError(f"sauvegarde illisible : {type(exc).__name__}") from exc
    if not saine:
        raise BackupError(f"integrity_check={verdict or 'vide'} sur {copie}")

    return {
        "path": str(copie),
        "size": taille,
        "sha256": sha256_of(copie),
        "integrity_check": "ok",
    }


def verified_backup(db_path: str, label: str) -> dict[str, object]:
    """Sauvegarde horodatee + verification complete. Leve si douteuse.

    Rend {path, size, sha256, integrity_check}. L'appelant journalise ce
    dictionnaire tel quel : il ne contient rien de confidentiel.
    """
    source_path = Path(db_path)
    if not source_path.exists():
        raise BackupError(f"base introuvable : {source_path}")

    root = source_path.resolve().parent / "backups"
    root.mkdir(parents=True, exist_ok=True)
    copie = root / f"demo-avant-{label}-{_stamp()}.db"
    if copie.exists():
        # Ceinture et bretelles : on n'ecrase jamais une sauvegarde, meme
        # en cas d'horloge figee ou de collision improbable.
        raise BackupError(f"une sauvegarde porte deja ce nom : {copie}")

    # 1. La copie elle-meme, par l'API SQLite (coherente a chaud).
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(copie))
        try:
            with target:
                source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    rapport = verify_backup_file(copie)
    logger.info(
        "Sauvegarde SQLite verifiee | chemin=%s | taille=%s octets | "
        "sha256=%s | integrity_check=%s",
        rapport["path"], rapport["size"], rapport["sha256"],
        rapport["integrity_check"],
    )
    return rapport
