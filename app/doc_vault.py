"""Coffre persistant des PDF extraits d'une archive.

Pourquoi il existe : un PDF contenu dans un ZIP n'est PAS une piece jointe
Gmail. Quand le client cliquait "Valider" plusieurs minutes plus tard, le bot
cherchait ce PDF parmi les pieces jointes de l'email et repondait
"Piece jointe introuvable dans l'email d'origine". Le PDF est donc ecrit ici,
dans le volume du conteneur, des le premier traitement.

Garanties :
  - le fichier survit a un redemarrage du conteneur (volume Docker) ;
  - il est cloisonne par `chat_id` : un client ne peut pas atteindre le
    fichier d'un autre ;
  - il est RELU sous controle d'empreinte : un fichier dont le SHA-256 ne
    correspond plus est considere comme absent, jamais comme valide ;
  - il n'est jamais indispensable : s'il manque, l'appelant retelecharge
    l'archive parente depuis Gmail.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

logger = logging.getLogger("demo_bot.doc_vault")

_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def vault_root(db_path: str) -> Path:
    """Repertoire du coffre, a cote de la base, donc dans le meme volume."""
    return Path(db_path).resolve().parent / "documents"


def _path_for(db_path: str, chat_id: int, doc_key: str) -> Path:
    """Chemin d'UN document. Les deux composants sont assainis : aucune
    valeur venue de l'exterieur ne peut faire sortir du repertoire."""
    chat = _SAFE.sub("", str(chat_id)) or "inconnu"
    key = _SAFE.sub("", str(doc_key))[:64]
    return vault_root(db_path) / chat / f"{key}.pdf"


def save(db_path: str, chat_id: int, doc_key: str, content: bytes) -> str:
    """Ecrit le PDF dans le coffre et renvoie son chemin (vide si echec).

    L'ecriture passe par un fichier temporaire puis un remplacement atomique :
    un conteneur tue au mauvais moment ne laisse jamais un PDF tronque que
    l'on relirait ensuite comme valide.
    """
    if not content:
        return ""
    target = _path_for(db_path, chat_id, doc_key)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".part")
        temporary.write_bytes(content)
        temporary.replace(target)
    except OSError as exc:
        logger.warning("PDF non conserve dans le coffre (%s): %s", doc_key[:12], exc)
        return ""
    return str(target)


def load(db_path: str, chat_id: int, doc_key: str, expected_sha256: str = "") -> bytes | None:
    """Relit le PDF, ou None s'il manque ou si son empreinte a change."""
    target = _path_for(db_path, chat_id, doc_key)
    try:
        content = target.read_bytes()
    except OSError:
        return None
    if expected_sha256:
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_sha256:
            logger.warning(
                "Empreinte du coffre differente pour %s : fichier ignore", doc_key[:12]
            )
            return None
    return content


def discard(db_path: str, chat_id: int, doc_key: str) -> None:
    """Supprime un fichier devenu inutile. L'absence n'est pas une erreur."""
    try:
        _path_for(db_path, chat_id, doc_key).unlink(missing_ok=True)
    except OSError:  # noqa: BLE001 - le coffre n'est jamais bloquant
        pass
