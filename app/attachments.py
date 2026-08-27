"""Pieces jointes d'un email : PDF, ZIP, et cle d'idempotence.

Un email peut porter plusieurs PDF, ou un ZIP en contenant plusieurs. Chaque
document est traite INDEPENDAMMENT : une erreur sur l'un ne doit jamais
empecher les autres d'aboutir.

Securite du depaquetage ZIP - toutes les limites sont configurables :
  - aucun chemin absolu, aucun ".." (zip slip) ;
  - aucun lien symbolique ;
  - uniquement des PDF, verifies par leur signature binaire, pas par leur
    extension ;
  - nombre de fichiers, taille par fichier, taille totale decompressee et
    taux de compression bornes (zip bomb) ;
  - profondeur bornee (un ZIP dans un ZIP) ;
  - ZIP chiffres refuses.
"""
from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field

PDF_MAGIC = b"%PDF"

# Limites par defaut, surchargeables par la configuration.
#
# MAX_FILES est passe de 25 a 120 : un ZIP comptable mensuel de 38
# pieces etait tronque en silence a 25, et les 13 documents restants
# n'ont jamais ete lus. La marge tient compte des envois annuels.
#
# MAX_TOTAL_BYTES reste DELIBEREMENT a 60 Mo. Relever le nombre de
# fichiers sans relever le volume total garde la protection anti-bombe
# exactement aussi serree qu'avant : 120 fichiers ne peuvent toujours
# pas depasser 60 Mo decompresses.
MAX_FILES = 120
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024
MAX_DEPTH = 2
MAX_COMPRESSION_RATIO = 200


class AttachmentError(RuntimeError):
    """Piece jointe inexploitable. Le message ne porte jamais de secret."""


@dataclass
class ZipLimits:
    max_files: int = MAX_FILES
    max_file_bytes: int = MAX_FILE_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_depth: int = MAX_DEPTH
    max_compression_ratio: int = MAX_COMPRESSION_RATIO


@dataclass
class DocumentFile:
    """Un PDF pret a etre analyse, et d'ou il vient.

    `member_path` est le chemin INTERNE du fichier dans l'archive parente
    ("factures/2026/achat.pdf"), vide pour une piece jointe directe. C'est
    lui, et non un identifiant Gmail, qui permet de retrouver plus tard
    exactement ce PDF dans le ZIP d'origine : les `attachmentId` renvoyes
    par Gmail changent d'une lecture a l'autre, le chemin interne jamais.
    """

    filename: str
    content: bytes
    source: str                 # "attachment" ou "zip"
    container: str = ""         # nom du ZIP d'origine, le cas echeant
    depth: int = 0
    member_path: str = ""

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def display_name(self) -> str:
        return f"{self.container}:{self.filename}" if self.container else self.filename

    @property
    def stable_ref(self) -> str:
        """Reference STABLE du document a l'interieur de son email.

        Elle ne depend que de noms de fichiers : deux lectures du meme email
        donnent la meme reference, donc la meme cle d'idempotence, donc la
        reprise retrouve son document au lieu d'en creer un nouveau.
        """
        if self.member_path:
            return f"{self.container}/{self.member_path}"
        return self.filename


@dataclass
class ExtractionReport:
    """Ce qui a ete accepte, et ce qui a ete refuse avec la raison."""

    files: list[DocumentFile] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    # Nombre de fichiers ECARTES par la seule limite de comptage. Compte a
    # part, parce que ce rejet-la n'est pas de meme nature que les autres :
    # le document etait valide, il a juste ete refuse par un plafond. Le
    # noyer parmi les rejets ordinaires est ce qui l'a rendu invisible.
    truncated: int = 0

    def reject(self, name: str, reason: str) -> None:
        self.rejected.append((name, reason))

    def reject_over_limit(self, name: str, limit: int) -> None:
        self.truncated += 1
        self.rejected.append((name, f"limite de {limit} fichiers atteinte"))


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def is_pdf(content: bytes) -> bool:
    """Un PDF se reconnait a sa signature, jamais a son extension."""
    return content[:4] == PDF_MAGIC


def is_zip(content: bytes) -> bool:
    return content[:2] == b"PK"


def idempotency_key(
    telegram_user_id: str, gmail_message_id: str, attachment_ref: str, file_sha256: str
) -> str:
    """Cle d'idempotence d'UN document.

    Elle inclut le client, l'email, la reference de la piece jointe ET le
    contenu : cinq PDF dans un meme email donnent cinq cles distinctes, et
    le meme PDF renvoye dans un autre email est reconnu par son empreinte.

    `attachment_ref` doit etre STABLE dans le temps (nom de fichier, chemin
    interne du ZIP). Elle a longtemps recu l'`attachmentId` de Gmail, qui
    change a chaque lecture du message : la meme piece jointe recevait alors
    une cle differente a chaque cycle, la reprise ne retrouvait plus son
    document et les boutons de validation repondaient "piece jointe
    introuvable".
    """
    raw = "|".join((
        (telegram_user_id or "").strip(),
        (gmail_message_id or "").strip(),
        (attachment_ref or "").strip(),
        (file_sha256 or "").strip(),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_safe_member_name(name: str) -> bool:
    """Refuse chemin absolu, remontee de repertoire et separateurs Windows."""
    if not name or name.endswith("/"):
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return False
    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        return False
    resolved = posixpath.normpath(normalized)
    return not (resolved.startswith("/") or resolved.startswith(".."))


def extract_pdfs_from_zip(
    content: bytes,
    *,
    container: str = "archive.zip",
    limits: ZipLimits | None = None,
    depth: int = 0,
    report: ExtractionReport | None = None,
    member_prefix: str = "",
) -> ExtractionReport:
    """Extrait les PDF d'un ZIP, en refusant tout ce qui est dangereux."""
    limits = limits or ZipLimits()
    report = report or ExtractionReport()

    if depth > limits.max_depth:
        report.reject(container, f"profondeur d'imbrication superieure a {limits.max_depth}")
        return report

    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        report.reject(container, f"archive illisible ({exc})")
        return report

    total = 0
    accepted = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if accepted >= limits.max_files:
            report.reject_over_limit(name, limits.max_files)
            continue
        if not _is_safe_member_name(name):
            report.reject(name, "chemin non sur (absolu, remontee ou lien)")
            continue
        if info.flag_bits & 0x1:
            report.reject(name, "archive chiffree : contenu non verifiable")
            continue
        if info.file_size > limits.max_file_bytes:
            report.reject(name, f"fichier trop volumineux ({info.file_size} octets)")
            continue
        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > limits.max_compression_ratio:
                report.reject(name, f"taux de compression suspect ({ratio:.0f}x)")
                continue
        if total + info.file_size > limits.max_total_bytes:
            report.reject(name, "taille totale decompressee depassee")
            continue

        try:
            with archive.open(info) as handle:
                data = handle.read(limits.max_file_bytes + 1)
        except Exception as exc:  # noqa: BLE001 - membre corrompu
            report.reject(name, f"lecture impossible ({type(exc).__name__})")
            continue
        if len(data) > limits.max_file_bytes:
            report.reject(name, "taille reelle superieure a la taille annoncee")
            continue

        total += len(data)
        base = posixpath.basename(name.replace("\\", "/"))

        member_path = f"{member_prefix}{name.replace(chr(92), '/')}"

        if is_zip(data):
            extract_pdfs_from_zip(
                data, container=f"{container}:{base}", limits=limits,
                depth=depth + 1, report=report, member_prefix=f"{member_path}/",
            )
            continue
        if not is_pdf(data):
            report.reject(name, "n'est pas un PDF (signature invalide)")
            continue

        accepted += 1
        report.files.append(
            DocumentFile(
                filename=base, content=data, source="zip",
                container=container, depth=depth + 1, member_path=member_path,
            )
        )
    return report


def collect_documents(
    filename: str, content: bytes, *, limits: ZipLimits | None = None
) -> ExtractionReport:
    """Transforme UNE piece jointe en 0..N documents analysables."""
    report = ExtractionReport()
    if is_zip(content):
        return extract_pdfs_from_zip(content, container=filename, limits=limits, report=report)
    if is_pdf(content):
        report.files.append(DocumentFile(filename=filename, content=content, source="attachment"))
        return report
    report.reject(filename, "piece jointe ignoree : ni PDF ni archive ZIP")
    return report


def extract_member(content: bytes, member_path: str) -> bytes | None:
    """Extrait UN SEUL membre d'une archive, par son chemin interne.

    Sert a la validation humaine : plutot que de redecompresser tout le pack
    pour retrouver un PDF, on ne sort que celui qui est demande. Traverse les
    archives imbriquees ("pack.zip/2026/facture.pdf" ou le premier segment
    est lui-meme une archive). Renvoie None si le chemin n'existe pas, sans
    jamais lever : l'appelant decidera quoi faire.
    """
    wanted = (member_path or "").replace("\\", "/").strip("/")
    if not wanted or not is_zip(content):
        return None
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return None
    names = {info.filename.replace("\\", "/"): info for info in archive.infolist()}

    info = names.get(wanted)
    if info is not None and not info.is_dir() and _is_safe_member_name(info.filename):
        with archive.open(info) as handle:
            return handle.read(MAX_FILE_BYTES + 1)

    # Archive imbriquee : le chemin demande commence par un membre ZIP.
    for name, entry in names.items():
        if entry.is_dir() or not wanted.startswith(f"{name}/"):
            continue
        if not _is_safe_member_name(entry.filename):
            continue
        with archive.open(entry) as handle:
            nested = handle.read(MAX_FILE_BYTES + 1)
        if not is_zip(nested):
            continue
        found = extract_member(nested, wanted[len(name) + 1:])
        if found is not None:
            return found
    return None
