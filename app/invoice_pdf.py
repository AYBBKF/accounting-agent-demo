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
    missing: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.missing

    @property
    def needs_human_review(self) -> bool:
        return bool(self.missing or self.anomalies)


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
            if nxt.strip():
                return parse_money(nxt)
    return None


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
