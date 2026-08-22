"""Extraction DETERMINISTE des champs d'une facture PDF.

Aucun modele de langage n'intervient ici : tous les champs sont lus par
expressions regulieres sur la couche texte du PDF, et tous les montants
sont manipules en Decimal. Un champ introuvable vaut None et est signale
comme manquant - il n'est JAMAIS devine.

Le controle HT + TVA = TTC est recalcule en Decimal : un ecart est
remonte comme anomalie plutot que silencieusement accepte.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

logger = logging.getLogger("demo_bot.invoice_pdf")


class InvoicePdfError(RuntimeError):
    """PDF illisible ou sans couche texte exploitable."""


def _strip_accents(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn"
    )


def _norm(value: str) -> str:
    """Majuscules, sans accents, espaces normalises : sert aux comparaisons."""
    return re.sub(r"\s+", " ", _strip_accents(value)).strip().upper()


_AMOUNT_RE = re.compile(r"(-?\d[\d\s  .,]*\d|\d)\s*(MAD|EUR|USD|DH|DHS)?", re.I)


def parse_money(text: str) -> tuple[Decimal, str] | None:
    """Parse un montant ("4 000.00 MAD", "1 500,00", "800.00 MAD").

    Retourne (montant, devise) ou None si la chaine ne contient pas de
    montant exploitable. Ne devine jamais une valeur.
    """
    if not text:
        return None
    m = _AMOUNT_RE.search(text.strip())
    if not m:
        return None
    raw, currency = m.group(1), (m.group(2) or "").upper()
    cleaned = re.sub(r"[\s  ]", "", raw)
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".") if re.search(r",\d{1,2}$", cleaned) else cleaned.replace(",", "")
    try:
        return Decimal(cleaned), currency
    except InvalidOperation:
        return None


def parse_fr_date(text: str) -> date | None:
    """Parse une date au format 21/08/2026, 21-08-2026 ou 2026-08-21."""
    if not text:
        return None
    m = re.search(r"\b(\d{2})[/\-.](\d{2})[/\-.](\d{4})\b", text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


# Une ligne qui ne contient QUE un montant (et eventuellement une devise).
# Sert a ne jamais confondre un libelle contenant un chiffre ("Ramettes A4")
# avec la valeur d'un total.
_PURE_AMOUNT_LINE_RE = re.compile(
    r"^[\s  ]*-?\d[\d\s  .,]*(?:\s*(?:MAD|EUR|USD|DH|DHS))?[\s]*$", re.I
)


def _is_pure_amount_line(line: str) -> bool:
    return bool(_PURE_AMOUNT_LINE_RE.match(line))


@dataclass
class InvoiceLine:
    """Ligne de detail d'une facture. Aucun champ n'est devine."""

    description: str
    quantite: Decimal
    prix_unitaire_ht: Decimal
    taux_tva: Decimal | None
    total_ht: Decimal


@dataclass
class ExtractedInvoice:
    """Champs extraits d'une facture. Tout champ absent reste None."""

    numero: str | None = None
    date_facture: date | None = None
    date_echeance: date | None = None
    fournisseur: str | None = None
    client: str | None = None
    montant_ht: Decimal | None = None
    taux_tva: Decimal | None = None
    montant_tva: Decimal | None = None
    montant_ttc: Decimal | None = None
    devise: str = ""
    statut: str | None = None
    mode_paiement: str | None = None
    ice_fournisseur: str | None = None
    ice_client: str | None = None
    lignes: list[InvoiceLine] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    # Champs pour lesquels le PDF propose PLUSIEURS valeurs differentes :
    # on ne tranche jamais tout seul, on demande une validation humaine.
    ambigus: list[str] = field(default_factory=list)
    is_avoir: bool = False
    # Le document ressemble-t-il reellement a une facture ? Determine par le
    # CONTENU du PDF, pas par l'objet de l'email : la requete Gmail ne filtre
    # plus sur un marqueur de sujet.
    is_invoice: bool = False

    @property
    def is_complete(self) -> bool:
        return not self.missing

    @property
    def needs_human_review(self) -> bool:
        return bool(self.missing or self.anomalies or self.ambigus or self.is_avoir)


# Champs sans lesquels on refuse d'ecrire quoi que ce soit en comptabilite.
REQUIRED_FIELDS = ("numero", "date_facture", "fournisseur", "montant_ht", "montant_ttc")


def _value_after_label(lines: list[str], label: str, *, skip: int = 0) -> str | None:
    """Retourne la valeur associee a un libelle.

    Gere les deux mises en page rencontrees : "LABEL: valeur" sur la meme
    ligne, et "LABEL" seul avec la valeur sur la ligne suivante non vide.
    """
    target = _norm(label)
    for i, line in enumerate(lines):
        normalized = _norm(line)
        if not normalized.startswith(target):
            continue
        remainder = line.strip()[len(label):].lstrip(" :\t")
        if remainder:
            return remainder
        seen = 0
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                continue
            if seen < skip:
                seen += 1
                continue
            return nxt.strip()
    return None


def _amount_after_label(lines: list[str], label: str) -> tuple[Decimal, str] | None:
    """Comme _value_after_label, mais en prenant la DERNIERE occurrence :
    dans un tableau, les totaux figurent apres les lignes de detail."""
    target = _norm(label)
    for i in range(len(lines) - 1, -1, -1):
        normalized = _norm(lines[i])
        if not normalized.startswith(target):
            continue
        after = _norm(lines[i])[len(target):]
        parsed = parse_money(after)
        if parsed and re.search(r"\d", after):
            return parsed
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                continue
            # Uniquement une ligne qui EST un montant : sinon on prendrait
            # le "4" de "Ramettes papier A4" pour un total.
            return parse_money(nxt) if _is_pure_amount_line(nxt) else None
    return None


def _all_amounts_for_label(lines: list[str], label: str) -> list[Decimal]:
    """Toutes les valeurs distinctes proposees par le PDF pour un libelle.

    Sert uniquement a detecter l'ambiguite : si un document annonce deux
    "TOTAL TTC" differents, aucune des deux n'est retenue sans validation.
    """
    target = _norm(label)
    found: list[Decimal] = []
    for i, line in enumerate(lines):
        if not _norm(line).startswith(target):
            continue
        after = _norm(line)[len(target):]
        parsed = parse_money(after) if re.search(r"\d", after) else None
        if parsed is None:
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    continue
                parsed = parse_money(nxt) if _is_pure_amount_line(nxt) else None
                break
        if parsed is not None and parsed[0] not in found:
            found.append(parsed[0])
    return found


_ICE_RE = re.compile(r"\bICE\s*[:\-]?\s*([0-9]{6,20})\b", re.I)
_SUPPLIER_SECTIONS = ("EMETTEUR", "FOURNISSEUR", "VENDEUR")
_CLIENT_SECTIONS = ("CLIENT", "DESTINATAIRE", "ACHETEUR")


def extract_ices(lines: list[str]) -> tuple[str | None, str | None]:
    """(ICE fournisseur, ICE client), rattaches a la section qui les precede.

    Aucun ICE n'est attribue par defaut : si le document ne comporte pas de
    section identifiable, les deux valeurs restent None et la facture partira
    en validation humaine.
    """
    section: str | None = None
    supplier: str | None = None
    client: str | None = None
    for line in lines:
        normalized = _norm(line)
        if normalized in _SUPPLIER_SECTIONS:
            section = "supplier"
            continue
        if normalized in _CLIENT_SECTIONS:
            section = "client"
            continue
        m = _ICE_RE.search(line)
        if not m:
            continue
        value = m.group(1)
        if section == "supplier" and supplier is None:
            supplier = value
        elif section == "client" and client is None:
            client = value
    return supplier, client


# Un document n'est traite comme une facture que si son texte porte un
# marqueur explicite ET des donnees comptables exploitables. Un contrat, un
# releve, une plaquette commerciale ou un bon de livraison sont ainsi ignores
# sans jamais atteindre le classeur.
# Les marqueurs sont cherches en MOTS ENTIERS : sans cela "DEVIS" matcherait
# "DEVISE", present dans le pied de page de toute facture en MAD.
_INVOICE_MARKERS = (
    "FACTURE", "FACTURES", "INVOICE", "NOTE DE CREDIT", "AVOIR", "FATURA",
    "RECHNUNG",
)
_NON_INVOICE_MARKERS = (
    "BON DE COMMANDE", "BON DE LIVRAISON", "DEVIS", "PROFORMA", "CONTRAT",
    "RELEVE BANCAIRE", "PURCHASE ORDER", "DELIVERY NOTE", "QUOTATION",
)


def _has_marker(normalized_text: str, markers: tuple[str, ...]) -> bool:
    return any(
        re.search(rf"\b{re.escape(marker)}\b", normalized_text) for marker in markers
    )


def looks_like_invoice(
    result: "ExtractedInvoice", normalized_text: str, lines: list[str]
) -> bool:
    """Vrai si le PDF est bien une facture (ou un avoir).

    Conditions cumulatives, toutes deterministes :
      - le document se declare facture, soit par une ligne de titre ("FACTURE"
        seule sur sa ligne), soit par un marqueur dans le texte ;
      - au moins un montant total exploitable ;
      - au moins un element d'identification (numero ou date de facture).

    Un marqueur de document NON comptable (devis, bon de commande, contrat)
    l'emporte, sauf si le document porte un vrai titre de facture. Le simple
    libelle "DATE DE FACTURE" ne suffit alors pas : un devis en comporte un
    aussi. Mieux vaut ignorer une vraie facture que d'ecrire un devis dans
    la comptabilite.
    """
    titles = {_norm(line) for line in lines}
    declared_invoice = bool(titles & set(_INVOICE_MARKERS))
    if _has_marker(normalized_text, _NON_INVOICE_MARKERS) and not declared_invoice:
        return False
    if not declared_invoice and not _has_marker(normalized_text, _INVOICE_MARKERS):
        return False
    if result.montant_ttc is None and result.montant_ht is None:
        return False
    return result.numero is not None or result.date_facture is not None


_TABLE_HEADER_START = "DESCRIPTION"
_TABLE_TOTALS_LABELS = ("TOTAL HT", "TOTAL", "SOUS TOTAL", "SOUS-TOTAL", "TOTAL TTC", "TVA")


def extract_lines(lines: list[str]) -> list[InvoiceLine]:
    """Lignes de detail du tableau de la facture.

    Format attendu (une valeur par ligne dans la couche texte) :
    description, quantite, prix unitaire, taux TVA, total HT. Toute ligne
    qui ne respecte pas ce format arrete la lecture : mieux vaut zero ligne
    qu'une ligne inventee.
    """
    start = None
    for i, line in enumerate(lines):
        if _norm(line) == _TABLE_HEADER_START:
            for j in range(i + 1, min(i + 8, len(lines))):
                if _norm(lines[j]) == "TOTAL HT":
                    start = j + 1
                    break
            break
    if start is None:
        return []

    items: list[InvoiceLine] = []
    i = start
    while i + 5 <= len(lines):
        chunk = [c for c in lines[i:i + 5]]
        if len(chunk) < 5:
            break
        if _norm(chunk[0]) in _TABLE_TOTALS_LABELS or _norm(chunk[0]).startswith("TOTAL"):
            break
        qte = parse_money(chunk[1]) if _is_pure_amount_line(chunk[1]) else None
        pu = parse_money(chunk[2]) if _is_pure_amount_line(chunk[2]) else None
        rate_match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", chunk[3])
        total = parse_money(chunk[4]) if _is_pure_amount_line(chunk[4]) else None
        if qte is None or pu is None or total is None:
            break
        rate = parse_money(rate_match.group(1))[0] if rate_match else None
        items.append(
            InvoiceLine(
                description=chunk[0].strip(),
                quantite=qte[0],
                prix_unitaire_ht=pu[0],
                taux_tva=rate,
                total_ht=total[0],
            )
        )
        i += 5
    return items


def extract_invoice_fields(text: str) -> ExtractedInvoice:
    """Extrait les champs d'une facture depuis le texte d'un PDF."""
    if not text or not text.strip():
        raise InvoicePdfError(
            "Le PDF ne contient aucune couche texte exploitable "
            "(document scanne ?). Extraction impossible."
        )
    lines = text.splitlines()
    result = ExtractedInvoice()

    # --- numero -----------------------------------------------------------
    m = re.search(r"N[°ºo]\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-_/]{3,})", text, re.I)
    if not m:
        m = re.search(r"\b(FAC[-_][A-Z0-9\-_]+)\b", text, re.I)
    if m:
        result.numero = m.group(1).strip()

    # --- dates ------------------------------------------------------------
    result.date_facture = parse_fr_date(_value_after_label(lines, "DATE DE FACTURE") or "")
    echeance = _value_after_label(lines, "DATE D'ECHEANCE") or _value_after_label(lines, "DATE D ECHEANCE")
    result.date_echeance = parse_fr_date(echeance or "")

    # --- tiers ------------------------------------------------------------
    result.fournisseur = _value_after_label(lines, "EMETTEUR") or _value_after_label(lines, "FOURNISSEUR")
    result.client = _value_after_label(lines, "CLIENT")

    # --- montants ---------------------------------------------------------
    ht = _amount_after_label(lines, "TOTAL HT")
    ttc = _amount_after_label(lines, "TOTAL TTC")
    tva = None
    for i in range(len(lines) - 1, -1, -1):
        if re.match(r"^\s*TVA\s+\d", _norm(lines[i])):
            rate = re.search(r"(\d+(?:[.,]\d+)?)\s*%", lines[i])
            if rate:
                parsed_rate = parse_money(rate.group(1))
                if parsed_rate:
                    result.taux_tva = parsed_rate[0]
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    tva = parse_money(nxt)
                    break
            break

    for attr, parsed in (("montant_ht", ht), ("montant_tva", tva), ("montant_ttc", ttc)):
        if parsed:
            setattr(result, attr, parsed[0])
            if parsed[1] and not result.devise:
                result.devise = parsed[1]
    if not result.devise:
        m = re.search(r"^\s*(MAD|EUR|USD)\s*$", text, re.M)
        if m:
            result.devise = m.group(1)

    # --- statut / paiement ------------------------------------------------
    result.statut = _value_after_label(lines, "STATUT")
    mode = re.search(r"Mode\s*:\s*(.+)", text)
    if mode:
        result.mode_paiement = mode.group(1).strip()

    # --- identifiants fiscaux et lignes de detail -------------------------
    result.ice_fournisseur, result.ice_client = extract_ices(lines)
    result.lignes = extract_lines(lines)

    # --- nature du document et avoir --------------------------------------
    normalized_text = _norm(text)
    result.is_invoice = looks_like_invoice(result, normalized_text, lines)
    result.is_avoir = bool(
        re.search(r"\bAVOIR\b|\bNOTE DE CREDIT\b|\bFACTURE D AVOIR\b", normalized_text)
        or (result.montant_ttc is not None and result.montant_ttc < 0)
    )

    # --- ambiguites -------------------------------------------------------
    # Un document qui annonce deux valeurs differentes pour un meme champ
    # critique n'est jamais arbitre automatiquement.
    for label, field_name in (("TOTAL HT", "montant_ht"), ("TOTAL TTC", "montant_ttc")):
        if len(_all_amounts_for_label(lines, label)) > 1:
            result.ambigus.append(field_name)
    numeros = []
    for pattern in (r"N[°ºo]\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-_/]{3,})", r"\b(FAC[-_][A-Z0-9\-_]+)\b"):
        for candidate in re.findall(pattern, text, re.I):
            candidate = candidate.strip().upper()
            if candidate not in numeros:
                numeros.append(candidate)
    if len(numeros) > 1:
        result.ambigus.append("numero")

    # --- controles --------------------------------------------------------
    result.missing = [f for f in REQUIRED_FIELDS if getattr(result, f) is None]

    if result.montant_ht is not None and result.montant_tva is not None and result.montant_ttc is not None:
        expected = result.montant_ht + result.montant_tva
        if expected != result.montant_ttc:
            result.anomalies.append(
                f"HT + TVA = {expected} mais TTC indique = {result.montant_ttc} "
                f"(ecart {abs(expected - result.montant_ttc)})"
            )
    if result.date_facture and result.date_echeance and result.date_echeance < result.date_facture:
        result.anomalies.append("la date d'echeance precede la date de facture")

    return result


def extract_from_pdf_bytes(data: bytes) -> ExtractedInvoice:
    """Lit un PDF en memoire et en extrait les champs."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependance manquante
        raise InvoicePdfError(f"Dependance pypdf manquante: {exc}") from exc
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - PDF corrompu / chiffre
        raise InvoicePdfError("PDF illisible ou corrompu.") from exc
    return extract_invoice_fields(text)
