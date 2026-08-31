"""Extraction structuree d'un document, avec provenance de chaque montant.

Regles non negociables :

  - aucun montant n'est invente ni recalcule par un modele de langage ;
  - tous les calculs et controles sont faits en `Decimal` ;
  - chaque montant conserve sa valeur, sa devise, le libelle source, la page
    et un niveau de confiance ;
  - une valeur DEDUITE (jamais lue telle quelle dans le document) porte le
    drapeau `inferred=True` et doit etre presentee comme deduite.

La couche texte des PDF du client coupe les valeurs sur plusieurs lignes :
un montant peut etre suivi de sa devise sur la ligne suivante
("24 800.00" puis " MAD"). Une passe de normalisation recolle ces lignes
avant toute analyse.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.doc_types import (
    BANK_STATEMENT,
    EXPORT_INVOICE,
    IMPORT_INVOICE,
    PAYMENT_RECEIPT,
    PENALTY_NOTICE,
    SIGNED_NEGATIVE_TYPES,
    Classification,
    classify,
    normalize,
)


class DocumentExtractError(RuntimeError):
    """Document sans couche texte exploitable, meme apres OCR."""


# Devises reconnues. Le code seul sur une ligne est recolle a la precedente.
CURRENCIES = ("MAD", "EUR", "USD", "GBP", "CHF", "DH", "DHS")
_CURRENCY_ONLY_RE = re.compile(rf"^\s*({'|'.join(CURRENCIES)})\s*$", re.I)
_AMOUNT_RE = re.compile(
    rf"(-?\d[\d\s  .,]*\d|-?\d)\s*({'|'.join(CURRENCIES)})?\b", re.I
)
# Un montant suivi d'un code devise, n'importe ou dans le document :
# un prix unitaire en USD sous un total en MAD reste une devise etrangere.
_MONEY_CODE_RE = re.compile(
    rf"\d[\d\s  .,]*\s*({'|'.join(CURRENCIES)})\b", re.I
)
_PURE_AMOUNT_RE = re.compile(
    rf"^[\s  ]*-?\d[\d\s  .,]*(?:\s*(?:{'|'.join(CURRENCIES)}))?[\s]*$",
    re.I,
)
_DATE_RE = re.compile(r"\b(\d{2})[/\-.](\d{2})[/\-.](\d{4})\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# L'ICE (identifiant commun d'entreprise, 15 chiffres au Maroc) peut etre
# etiquete de plusieurs facons sur une VRAIE facture : "ICE : 00234...",
# mais aussi "ICE fournisseur : 00234..." ou "ICE client : 00345...". On
# tolere donc un qualificatif (mot + separateurs) entre "ICE" et le nombre,
# sur la MEME ligne (pas de saut de ligne), sans jamais traverser vers un
# autre nombre. Les factures synthetiques n'utilisaient que "ICE :", ce qui
# masquait ce trou face aux factures reelles.
_ICE_RE = re.compile(r"\bICE\b[^\d\n]{0,25}?([0-9]{8,20})\b", re.I)


@dataclass
class Line:
    """Une ligne de texte et la page dont elle provient."""

    page: int
    text: str

    @property
    def norm(self) -> str:
        return normalize(self.text)


@dataclass
class Amount:
    """Un montant et sa provenance complete."""

    value: Decimal
    currency: str
    label: str                  # libelle source, tel qu'ecrit dans le document
    page: int
    confidence: float = 1.0
    inferred: bool = False      # True = valeur deduite, jamais lue telle quelle

    def signed(self, negative: bool) -> "Amount":
        """Meme montant, force en negatif (avoirs) ou positif."""
        magnitude = abs(self.value)
        return Amount(
            value=-magnitude if negative else magnitude,
            currency=self.currency, label=self.label, page=self.page,
            confidence=self.confidence, inferred=self.inferred,
        )


@dataclass
class DocumentLine:
    """Ligne de detail d'un document (facture, devis, bon de commande)."""

    description: str
    quantite: Decimal | None
    prix_unitaire: Decimal | None
    taux_tva: Decimal | None
    total: Decimal | None
    devise: str = ""
    hs_code: str = ""


@dataclass
class BankLine:
    """Operation d'un releve bancaire.

    Le sens (debit/credit) vient d'abord de la COLONNE reellement occupee
    par le montant dans le PDF, ensuite seulement de la variation du solde.
    Un montant dont la colonne reste indeterminee est conserve comme
    `mouvement` a valider : il n'est JAMAIS transforme en solde. Ecrire un
    mouvement dans la colonne "Solde" revenait a affirmer un solde que le
    document n'annonce nulle part.
    """

    date_operation: date | None
    libelle: str
    reference: str
    debit: Decimal | None
    credit: Decimal | None
    solde: Decimal | None
    devise: str
    page: int
    inferred_direction: bool = False
    # Montant lu dont le sens n'a pas pu etre etabli.
    mouvement: Decimal | None = None

    @property
    def sens_indetermine(self) -> bool:
        return self.debit is None and self.credit is None and self.mouvement is not None


@dataclass
class ExtractedDocument:
    """Tout ce qui a pu etre lu, plus ce qui manque et ce qui cloche."""

    classification: Classification
    text_source: str = "native"          # "native", "ocr" ou "vision:<niveau>"
    raw_text: str = ""                   # texte lu, pour une relecture eventuelle
    pages: int = 1

    numero: str | None = None
    # Identifiant INTERNE deterministe, genere par le pipeline pour un recu
    # sans numero externe : (entreprise, email, membre, empreinte). Jamais
    # presente comme un numero legal du fournisseur.
    numero_interne: str | None = None
    date_document: date | None = None
    date_echeance: date | None = None

    emetteur: str | None = None
    emetteur_ice: str | None = None
    destinataire: str | None = None
    destinataire_ice: str | None = None

    montant_ht: Amount | None = None
    montant_tva: Amount | None = None
    montant_ttc: Amount | None = None
    montant_paye: Amount | None = None
    frais_annexes: Amount | None = None      # fret, assurance a l'import
    taux_tva: Decimal | None = None
    devise: str = ""

    statut: str | None = None
    mode_paiement: str | None = None
    facture_liee: str | None = None
    # Valeur BRUTE du champ "facture d'origine", conservee pour le journal
    # meme quand elle n'est pas une reference exploitable.
    facture_liee_brute: str = ""
    # Codes devise reellement lus sur les montants du document.
    devises_detectees: list[str] = field(default_factory=list)
    motif: str = ""

    # Commerce international
    incoterm: str = ""
    pays_origine: str = ""
    pays_destination: str = ""

    lignes: list[DocumentLine] = field(default_factory=list)
    bank_lines: list[BankLine] = field(default_factory=list)

    missing: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    ambigus: list[str] = field(default_factory=list)

    @property
    def doc_type(self) -> str:
        return self.classification.doc_type

    @property
    def amounts(self) -> list[Amount]:
        return [a for a in (self.montant_ht, self.montant_tva, self.montant_ttc,
                            self.montant_paye) if a is not None]

    @property
    def confidence(self) -> float:
        """Confiance globale : la plus faible du type et des montants."""
        scores = [self.classification.confidence] + [a.confidence for a in self.amounts]
        return min(scores) if scores else 0.0


# --- primitives de lecture ------------------------------------------------

def parse_money(text: str) -> tuple[Decimal, str] | None:
    """Parse un montant et sa devise. Retourne None plutot que de deviner."""
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
        cleaned = (
            cleaned.replace(",", ".") if re.search(r",\d{1,2}$", cleaned)
            else cleaned.replace(",", "")
        )
    try:
        return Decimal(cleaned), ("MAD" if currency in ("DH", "DHS") else currency)
    except InvalidOperation:
        return None


def parse_date(text: str) -> date | None:
    if not text:
        return None
    m = _DATE_RE.search(text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    m = _ISO_DATE_RE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def build_lines(pages: list[str]) -> list[Line]:
    """Texte par page -> lignes numerotees, devises recollees.

    Un code devise seul sur sa ligne appartient au montant precedent : la
    mise en page PDF les separe, pas le document.
    """
    lines: list[Line] = []
    for page_no, page_text in enumerate(pages, start=1):
        for raw in (page_text or "").splitlines():
            if _CURRENCY_ONLY_RE.match(raw) and lines and lines[-1].page == page_no:
                lines[-1].text = f"{lines[-1].text.rstrip()} {raw.strip().upper()}"
                continue
            lines.append(Line(page=page_no, text=raw))
    return lines


def is_amount_line(line: Line) -> bool:
    return bool(_PURE_AMOUNT_RE.match(line.text))


def strip_label(raw: str, label: str) -> str:
    """Retire un libelle du DEBUT d'une ligne, accents compris.

    Le retrait ne peut pas se faire par expression reguliere sur le texte
    brut : "Numero de facture" ne matche pas "Numero de facture" accentue.
    La normalisation preservant le decoupage en mots, on retire donc autant
    de mots que le libelle en compte, puis la ponctuation residuelle et un
    eventuel second libelle ("Client / Importer : X").
    """
    words = raw.strip().split()
    label_words = normalize(label).split()
    remainder = " ".join(words[len(label_words):]).strip()
    remainder = re.sub(r"^[:/\-]\s*", "", remainder).strip()
    remainder = re.sub(r"^[A-Za-zÀ-ſ]+\s*:\s*", "", remainder).strip()
    return remainder


# Intitules de COLONNE reconnus. La lecture par colonne n'est tentee que sur
# ces cellules-la : se contenter de "ligne sans chiffre" prenait la VALEUR
# d'un libelle pour un en-tete ("Facture d'origine" / "NON PRECISEE") et
# rattachait l'avoir a un montant au lieu d'une facture.
_TABLE_HEADER_CELLS = frozenset({
    "NUMERO", "NUMERO DE FACTURE", "NUMERO DE PIECE", "NO", "N",
    "DATE", "DATE DE FACTURE", "DATE DU DOCUMENT", "DATE D'EMISSION",
    "ECHEANCE", "DATE D'ECHEANCE", "DATE LIMITE DE PAIEMENT",
    "REFERENCE", "LIBELLE", "LIBELLE / REFERENCE", "DEBIT", "CREDIT",
    "INVOICE NUMBER", "INVOICE DATE", "DUE DATE",
})


def _header_run(lines: list[Line], index: int) -> list[int]:
    """Bloc contigu de cellules d'EN-TETE auquel appartient `index`.

    Une cellule d'en-tete est une ligne dont le texte normalise est un
    intitule de colonne CONNU. Le bloc s'arrete a la premiere ligne qui n'en
    est pas un : les valeurs commencent la.
    """
    def is_header(k: int) -> bool:
        text = lines[k].text.strip()
        return bool(text) and normalize(text) in _TABLE_HEADER_CELLS

    if not is_header(index):
        return []
    start = index
    while start - 1 >= 0 and is_header(start - 1):
        start -= 1
    end = index
    while end + 1 < len(lines) and is_header(end + 1):
        end += 1
    return list(range(start, end + 1))


def column_table_value(lines: list[Line], index: int) -> tuple[str, int] | None:
    """Valeur d'un en-tete de tableau dispose en COLONNES.

    Mise en page tres courante des factures reelles : une ligne par cellule
    d'en-tete, puis une ligne par cellule de valeur.

        NUMERO / DATE / ECHEANCE
        F2026-1101 / 15/08/2026 / 15/09/2026

    Le libelle et sa valeur ne sont donc PAS voisins : ils sont separes par
    les autres en-tetes. Lire "la ligne suivante" rendait ECHEANCE comme date
    du document, et la facture partait en quarantaine "date illisible" alors
    que la date etait parfaitement lisible.

    On n'applique la lecture par colonne que si l'en-tete compte AU MOINS
    deux cellules (un vrai tableau) et si le bloc de valeurs qui suit a
    exactement la meme largeur. Sinon on ne devine rien.
    """
    header = _header_run(lines, index)
    if len(header) < 2:
        return None
    values: list[Line] = []
    for line in lines[header[-1] + 1:]:
        if not line.text.strip():
            continue
        values.append(line)
        if len(values) == len(header):
            break
    if len(values) != len(header):
        return None
    chosen = values[header.index(index)]
    return chosen.text.strip(), chosen.page


def value_after(lines: list[Line], label: str) -> tuple[str, int] | None:
    """Valeur associee a un libelle, avec la page ou elle a ete trouvee.

    Gere les trois mises en page rencontrees : "Libelle : valeur",
    "Libelle" seul avec la valeur sur la ligne suivante, et le tableau en
    colonnes (en-tetes groupes, puis valeurs groupees).
    """
    target = normalize(label)
    for i, line in enumerate(lines):
        n = line.norm
        if n != target and not n.startswith(target + " ") and not n.startswith(target + ":"):
            continue
        remainder = strip_label(line.text, label)
        if remainder:
            return remainder, line.page
        column = column_table_value(lines, i)
        if column is not None:
            return column
        for nxt in lines[i + 1:]:
            if nxt.text.strip():
                return nxt.text.strip(), nxt.page
        return None
    return None


def amount_after(lines: list[Line], label: str, *, last: bool = True) -> Amount | None:
    """Montant associe a un libelle, avec sa provenance.

    `last=True` prend la DERNIERE occurrence : dans un tableau, les totaux
    figurent apres les lignes de detail.
    """
    target = normalize(label)
    indexes = [i for i, l in enumerate(lines) if l.norm.startswith(target)]
    if not indexes:
        return None
    for i in reversed(indexes) if last else indexes:
        line = lines[i]
        remainder = line.norm[len(target):]
        if re.search(r"\d", remainder):
            parsed = parse_money(remainder)
            if parsed:
                return Amount(parsed[0], parsed[1], line.text.strip(), line.page)
        for nxt in lines[i + 1:]:
            if not nxt.text.strip():
                continue
            if not is_amount_line(nxt):
                break
            parsed = parse_money(nxt.text)
            if parsed:
                return Amount(parsed[0], parsed[1], line.text.strip(), nxt.page)
            break
    return None


def all_amounts_for(lines: list[Line], label: str) -> list[Decimal]:
    """Valeurs distinctes proposees pour un meme libelle (detection d'ambiguite)."""
    target = normalize(label)
    found: list[Decimal] = []
    for i, line in enumerate(lines):
        if not line.norm.startswith(target):
            continue
        remainder = line.norm[len(target):]
        parsed = parse_money(remainder) if re.search(r"\d", remainder) else None
        if parsed is None:
            for nxt in lines[i + 1:]:
                if not nxt.text.strip():
                    continue
                parsed = parse_money(nxt.text) if is_amount_line(nxt) else None
                break
        if parsed and parsed[0] not in found:
            found.append(parsed[0])
    return found


# Libelles de role qui precedent une raison sociale. Ils doivent etre
# retires du NOM : "Client : ATLAS CLINIQUE SARL" et "ATLAS CLINIQUE SARL"
# designent le meme tiers, et les laisser differer a suffi a creer une
# seconde fiche client pour une societe deja connue.
_ROLE_LABELS = (
    "CLIENT", "CLIENTE", "FOURNISSEUR", "EMETTEUR", "ACHETEUR", "VENDEUR",
    "IMPORTER", "EXPORTER", "SUPPLIER", "CUSTOMER", "VENDOR", "BUYER",
    "SELLER", "BILL TO", "SOLD TO", "SHIP TO", "BENEFICIAIRE", "TITULAIRE",
    "ORGANISME", "PAYEUR", "DESTINATAIRE", "RAISON SOCIALE",
)


def clean_party_name(raw: str | None) -> str | None:
    """Retire le libelle de role qui prefixe parfois une raison sociale.

    Applique APRES l'extraction, quel que soit le chemin qui a produit le
    nom : le libelle peut etre reste colle parce qu'il vivait sur la ligne
    suivante, pas sur la ligne du label. Sans ce nettoyage, le meme tiers
    entre deux fois dans le classeur sous deux identifiants differents.

    Un nom qui ne serait QUE le libelle n'est pas transforme en chaine
    vide silencieusement : on rend None, et la politique de decision
    tranchera - c'est elle qui sait qu'un tiers sans nom bloque tout.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return None
    for _ in range(3):  # "Client : Fournisseur : X" reste pathologique mais fini
        courant = normalize(candidate)
        retire = False
        for label in _ROLE_LABELS:
            if not courant.startswith(label):
                continue
            reste = candidate[len(label):].lstrip()
            reste = re.sub(r"^[:/\-]\s*", "", reste).strip()
            if reste and normalize(reste) != courant:
                candidate = reste
                retire = True
                break
        if not retire:
            break
    candidate = candidate.strip(" :;,-").strip()
    return candidate or None


def party(lines: list[Line], labels: tuple[str, ...]) -> tuple[str | None, str | None, int]:
    """(nom, ICE, page) d'une partie. L'ICE est celui du bloc, pas un autre."""
    for i, line in enumerate(lines):
        for label in labels:
            target = normalize(label)
            n = line.norm
            if n != target and not n.startswith(target + " :") and not n.startswith(target + " "):
                continue
            name = strip_label(line.text, label)
            if not name:
                for nxt in lines[i + 1:]:
                    if nxt.text.strip():
                        name = nxt.text.strip()
                        break
            ice = None
            for nxt in lines[i + 1:i + 8]:
                m = _ICE_RE.search(nxt.text)
                if m:
                    ice = m.group(1)
                    break
                # On s'arrete au bloc SUIVANT, reconnu par une ligne qui est
                # le libelle de role lui-meme. Un simple `startswith` coupait
                # la recherche sur la raison sociale du tiers quand elle
                # commence par ce mot ("CLIENT NOVA SARL") : l'ICE, ecrit
                # deux lignes plus bas, n'etait alors jamais lu.
                voisin = normalize(nxt.text)
                if any(voisin == normalize(l) or voisin.startswith(normalize(l) + " :")
                       or voisin.startswith(normalize(l) + ":")
                       for l in ("CLIENT", "FOURNISSEUR", "EMETTEUR", "ACHETEUR",
                                 "IMPORTER", "BENEFICIAIRE", "EXPORTER",
                                 "DESTINATAIRE", "VENDEUR", "TITULAIRE",
                                 "PAYEUR")):
                    break
            return (name or None), ice, line.page
    return None, None, 1


# --- lignes de detail -----------------------------------------------------

_COLUMN_ALIASES = {
    "DESIGNATION": "description", "DESCRIPTION": "description",
    "QTE": "quantite", "QTY": "quantite", "QUANTITE": "quantite",
    "PRIX UNITAIRE HT": "prix_unitaire", "PRIX UNITAIRE": "prix_unitaire",
    "UNIT PRICE": "prix_unitaire",
    "MONTANT HT": "total", "MONTANT": "total", "AMOUNT": "total",
    "TOTAL HT": "total", "TOTAL": "total",
    "TVA": "taux_tva",
    "HS CODE": "hs_code", "CODE SH": "hs_code",
}
_TOTALS_PREFIXES = (
    "TOTAL", "SOUS TOTAL", "SOUS-TOTAL", "TVA", "GOODS VALUE", "FREIGHT",
    "MONTANT TOTAL", "NET A PAYER", "PENALITE", "MAJORATION",
)


def extract_detail_lines(lines: list[Line]) -> list[DocumentLine]:
    """Lignes du tableau de detail, pilotees par les en-tetes reels.

    Le nombre de colonnes varie selon le document (4 pour une facture
    nationale, 5 avec un code SH a l'import). Il est lu dans l'en-tete, pas
    suppose. Toute ligne qui ne respecte pas le format arrete la lecture :
    mieux vaut zero ligne qu'une ligne inventee.
    """
    start = None
    columns: list[str] = []
    for i, line in enumerate(lines):
        if _COLUMN_ALIASES.get(line.norm) != "description":
            continue
        columns = ["description"]
        j = i + 1
        while j < len(lines):
            role = _COLUMN_ALIASES.get(lines[j].norm)
            if role is None:
                break
            columns.append(role)
            j += 1
        if len(columns) >= 3:
            start = j
            break
        columns = []
    if start is None or not columns:
        return []

    width = len(columns)
    items: list[DocumentLine] = []
    buffer: list[Line] = []
    for line in lines[start:]:
        if not line.text.strip():
            continue
        if not buffer and any(line.norm.startswith(p) for p in _TOTALS_PREFIXES):
            break
        buffer.append(line)
        if len(buffer) < width:
            continue
        values = dict(zip(columns, buffer))
        description = values["description"].text.strip()
        qty = parse_money(values["quantite"].text) if "quantite" in values else None
        unit = parse_money(values["prix_unitaire"].text) if "prix_unitaire" in values else None
        total = parse_money(values["total"].text) if "total" in values else None
        rate = None
        if "taux_tva" in values:
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", values["taux_tva"].text)
            rate = parse_money(m.group(1))[0] if m else None
        if total is None:
            break
        items.append(
            DocumentLine(
                description=description,
                quantite=qty[0] if qty else None,
                prix_unitaire=unit[0] if unit else None,
                taux_tva=rate,
                total=total[0],
                devise=total[1],
                hs_code=values["hs_code"].text.strip() if "hs_code" in values else "",
            )
        )
        buffer = []
    return items


def extract_vat(lines: list[Line]) -> tuple[Decimal | None, Amount | None]:
    """(taux, montant) de TVA.

    Piege reel du classeur client : le libelle du total de TVA porte deja le
    taux ("TVA 20 %"). Lire le premier nombre de cette ligne donnerait un
    montant de TVA de 20,00 MAD. Le taux est donc extrait, puis EXCLU, et le
    montant est cherche sur la ligne suivante.
    """
    rate: Decimal | None = None
    amount: Amount | None = None
    for i, line in enumerate(lines):
        # La ligne de TVA est reconnue par sa FORME ("TVA ... %"), pas par la
        # syntaxe du taux. Un taux illisible ("TVA 2E+1 %", taux barre,
        # scanne de travers) ne doit pas faire disparaitre la ligne entiere :
        # sinon le MONTANT de TVA n'est plus lu, le controle
        # "HT + TVA = TTC" ne s'execute plus, et une facture aux totaux
        # incoherents passe en comptabilite sans etre vue. On lit donc le
        # montant meme quand le taux reste inconnu.
        m = re.match(r"^TVA\b(?P<taux>[^%\d]*(?:\d[^%]*)?)%(?P<reste>.*)$", line.norm)
        if not m:
            continue
        candidat = (m.group("taux") or "").strip()
        parsed_rate = parse_money(candidat) if re.fullmatch(r"\d+(?:[.,]\d+)?", candidat) else None
        if parsed_rate:
            rate = parsed_rate[0]
        trailing = m.group("reste")
        if re.search(r"\d", trailing):
            parsed = parse_money(trailing)
            if parsed:
                amount = Amount(parsed[0], parsed[1], line.text.strip(), line.page)
        if amount is None:
            for nxt in lines[i + 1:]:
                if not nxt.text.strip():
                    continue
                if is_amount_line(nxt):
                    parsed = parse_money(nxt.text)
                    if parsed:
                        amount = Amount(parsed[0], parsed[1], line.text.strip(), nxt.page)
                break
        break
    if amount is None:
        amount = amount_after(lines, "TVA")
        if amount is not None and "%" in (amount.label or ""):
            # La ligne relue porte un pourcentage : c'est le TAUX, pas un
            # montant. Le retenir donnait une TVA de 1,00 MAD et fabriquait
            # un faux ecart "HT + TVA != TTC" sur des factures saines.
            amount = None
        if amount is not None and rate is not None and abs(amount.value) == rate:
            # On a relu le taux, pas un montant : on prefere ne rien affirmer.
            amount = None
    return rate, amount


# --- releve bancaire ------------------------------------------------------

def extract_bank_lines(lines: list[Line], devise: str) -> tuple[list[BankLine], list[str]]:
    """Operations d'un releve bancaire, sens deduit de la variation du solde.

    La couche texte ne conserve pas les colonnes : impossible de savoir si
    "7 500.00 MAD" est un debit ou un credit par sa position. Le sens est
    donc calcule a partir du solde courant, puis RECOUPE avec le montant lu.
    Si les deux ne concordent pas, la ligne est signalee plutot que devinee.
    """
    anomalies: list[str] = []
    entries: list[BankLine] = []
    blocks: list[tuple[Line, list[Line]]] = []
    current: tuple[Line, list[Line]] | None = None
    for line in lines:
        if not line.text.strip():
            continue
        if _DATE_RE.fullmatch(line.text.strip()):
            if current:
                blocks.append(current)
            current = (line, [])
        elif current is not None:
            current[1].append(line)
    if current:
        blocks.append(current)

    previous_balance: Decimal | None = None
    for date_line, body in blocks:
        operation_date = parse_date(date_line.text)
        amounts = [parse_money(l.text) for l in body if is_amount_line(l)]
        amounts = [a for a in amounts if a is not None]
        labels = [l.text.strip() for l in body if not is_amount_line(l)]
        if not amounts:
            continue
        libelle = labels[0] if labels else ""
        balance = amounts[-1][0]
        currency = next((a[1] for a in amounts if a[1]), devise)

        debit = credit = None
        inferred = False
        if len(amounts) >= 2:
            movement = amounts[-2][0]
            if previous_balance is None:
                anomalies.append(
                    f"operation du {operation_date} sans solde de depart connu : sens non determine"
                )
            else:
                delta = balance - previous_balance
                inferred = True
                if abs(delta) != abs(movement):
                    anomalies.append(
                        f"operation du {operation_date} : mouvement lu {movement} "
                        f"incoherent avec la variation de solde {delta}"
                    )
                elif delta > 0:
                    credit = abs(movement)
                elif delta < 0:
                    debit = abs(movement)
        previous_balance = balance

        reference = bank_reference(libelle)
        entries.append(
            BankLine(
                date_operation=operation_date, libelle=libelle, reference=reference,
                debit=debit, credit=credit, solde=balance, devise=currency,
                page=date_line.page, inferred_direction=inferred,
            )
        )
    return entries, anomalies


# Reference de piece citee dans un libelle bancaire. Deux formes reelles :
# "REL-BP-2026-08" (lettres puis separateur) et "F2026-1101" (une a quatre
# lettres collees a l'annee). Ne reconnaitre que la premiere laissait tous
# les virements de facture sans reference, donc sans rapprochement possible.
_BANK_REFERENCE_RES = (
    re.compile(r"\b([A-Z]{2,}[-_][A-Z0-9\-_]+)\b"),
    re.compile(r"\b([A-Z]{1,4}\d{2,}[-_][A-Z0-9]{2,})\b"),
)


def bank_reference(libelle: str) -> str:
    """Reference de document citee par un libelle bancaire, sinon "" ."""
    texte = (libelle or "").upper()
    for motif in _BANK_REFERENCE_RES:
        found = motif.search(texte)
        if found:
            return found.group(1)
    return ""


# --- releve bancaire : lecture PAR COLONNES --------------------------------


@dataclass
class Cell:
    """Un fragment de texte du PDF avec sa position reelle sur la page."""

    page: int
    x: float
    y: float
    text: str

    @property
    def norm(self) -> str:
        return normalize(self.text)


def read_pdf_cells(data: bytes) -> list[Cell]:
    """Fragments de texte avec leurs coordonnees.

    La couche texte d'un PDF conserve la position de chaque fragment ; c'est
    `extract_text()` qui l'aplatit. Sans elle, impossible de savoir si
    "6 000,00 MAD" est ecrit dans la colonne Debit ou dans la colonne
    Credit - et le sens de l'operation etait alors perdu.
    """
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependance manquante
        return []
    import io

    cellules: list[Cell] = []
    try:
        reader = PdfReader(io.BytesIO(data))
        for numero, page in enumerate(reader.pages, start=1):
            def visiteur(text, cm, tm, font, size, _page=numero):
                contenu = (text or "").strip()
                if contenu:
                    cellules.append(Cell(page=_page, x=float(tm[4]), y=float(tm[5]), text=contenu))

            page.extract_text(visitor_text=visiteur)
    except Exception:  # noqa: BLE001 - PDF corrompu : on retombe sur le texte plat
        return []
    return cellules


# Tolerance verticale pour regrouper des fragments sur une meme ligne.
_ROW_TOLERANCE = 3.0

_BANK_HEADERS = {"DEBIT": "debit", "CREDIT": "credit", "SOLDE": "solde"}


def _group_rows(cells: list[Cell]) -> list[list[Cell]]:
    lignes: list[list[Cell]] = []
    for cellule in sorted(cells, key=lambda c: (c.page, -c.y, c.x)):
        if lignes and lignes[-1][0].page == cellule.page and abs(lignes[-1][0].y - cellule.y) <= _ROW_TOLERANCE:
            lignes[-1].append(cellule)
        else:
            lignes.append([cellule])
    for ligne in lignes:
        ligne.sort(key=lambda c: c.x)
    return lignes


def _bank_columns(rows: list[list[Cell]]) -> tuple[dict[str, float], int]:
    """Position en x des colonnes Debit / Credit / Solde, et l'index de l'en-tete."""
    for index, ligne in enumerate(rows):
        trouve = {}
        for cellule in ligne:
            role = _BANK_HEADERS.get(cellule.norm)
            if role:
                trouve[role] = cellule.x
        if "debit" in trouve and "credit" in trouve:
            return trouve, index
    return {}, -1


def _closest_column(x: float, colonnes: dict[str, float]) -> str | None:
    """Colonne dont l'origine precede le montant et lui est la plus proche.

    Un montant est cale a DROITE dans sa colonne : son x de depart est donc
    toujours superieur ou egal a celui de l'intitule, et inferieur a celui
    de la colonne suivante.
    """
    candidates = [(nom, ox) for nom, ox in colonnes.items() if x >= ox - 2.0]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[1])[0]


def extract_bank_lines_by_column(
    cells: list[Cell], devise: str
) -> tuple[list[BankLine], list[str]]:
    """Operations d'un releve, sens deduit de la COLONNE occupee.

    Chaque montant est rattache a la colonne Debit, Credit ou Solde selon sa
    position reelle. Un montant dont la colonne reste indeterminee est
    conserve comme mouvement a valider, avec une anomalie tracable : on ne
    fabrique jamais un solde, et une seule operation douteuse ne bloque pas
    le reste du releve.
    """
    rows = _group_rows(cells)
    colonnes, entete = _bank_columns(rows)
    if not colonnes or entete < 0:
        return [], []

    anomalies: list[str] = []
    operations: list[BankLine] = []
    for ligne in rows[entete + 1:]:
        dates = [c for c in ligne if _DATE_RE.fullmatch(c.text.strip())]
        montants = [c for c in ligne if is_amount_line(Line(page=c.page, text=c.text))]
        if not dates or not montants:
            continue
        operation_date = parse_date(dates[0].text)
        libelles = [c.text.strip() for c in ligne if c not in dates and c not in montants]
        libelle = libelles[0] if libelles else ""

        debit = credit = solde = mouvement = None
        courante = devise
        for cellule in montants:
            parsed = parse_money(cellule.text)
            if not parsed:
                continue
            valeur, monnaie = parsed
            courante = monnaie or courante
            colonne = _closest_column(cellule.x, colonnes)
            if colonne == "debit":
                debit = abs(valeur)
            elif colonne == "credit":
                credit = abs(valeur)
            elif colonne == "solde":
                solde = valeur
            else:
                mouvement = abs(valeur)
                anomalies.append(
                    f"operation du {operation_date} : montant {valeur} hors des colonnes "
                    "Debit et Credit, sens a valider"
                )

        reference = bank_reference(libelle)
        operations.append(
            BankLine(
                date_operation=operation_date, libelle=libelle, reference=reference,
                debit=debit, credit=credit, solde=solde, devise=courante,
                page=ligne[0].page, inferred_direction=False, mouvement=mouvement,
            )
        )
    return operations, anomalies


def check_bank_totals(doc: "ExtractedDocument", lines: list[Line]) -> list[str]:
    """Recoupe les lignes extraites avec les totaux annonces par le releve."""
    problems: list[str] = []
    declared_debit = amount_after(lines, "Total debits")
    declared_credit = amount_after(lines, "Total credits")
    closing = amount_after(lines, "Solde de cloture")
    total_debit = sum((l.debit for l in doc.bank_lines if l.debit), Decimal("0"))
    total_credit = sum((l.credit for l in doc.bank_lines if l.credit), Decimal("0"))
    if declared_debit and total_debit != abs(declared_debit.value):
        problems.append(
            f"total des debits extraits {total_debit} != total annonce {declared_debit.value}"
        )
    if declared_credit and total_credit != abs(declared_credit.value):
        problems.append(
            f"total des credits extraits {total_credit} != total annonce {declared_credit.value}"
        )
    if closing and doc.bank_lines:
        last = doc.bank_lines[-1].solde
        if last is not None and last != closing.value:
            problems.append(
                f"dernier solde extrait {last} != solde de cloture annonce {closing.value}"
            )
    return problems


# --- orchestration --------------------------------------------------------

# Champs sans lesquels on refuse d'ecrire quoi que ce soit, par type.
REQUIRED_FIELDS = {
    "facture_achat": ("numero", "date_document", "emetteur", "montant_ht", "montant_ttc"),
    "facture_vente": ("numero", "date_document", "destinataire", "montant_ht", "montant_ttc"),
    "avoir_fournisseur": ("numero", "date_document", "emetteur", "montant_ttc"),
    "avoir_client": ("numero", "date_document", "destinataire", "montant_ttc"),
    "facture_import": ("numero", "date_document", "emetteur", "montant_ttc"),
    "facture_export": ("numero", "date_document", "destinataire", "montant_ttc"),
    "avis_penalite": ("numero", "date_document", "montant_ttc", "date_echeance"),
    "recu_paiement": ("numero", "date_document", "montant_paye"),
    "releve_bancaire": ("date_document",),
    "devis": ("numero", "date_document"),
    "bon_commande": ("numero", "date_document"),
    "bon_livraison": ("numero", "date_document"),
    "inconnu": (),
}

_NUMBER_LABELS = (
    "Numero de facture", "Numero du recu", "Numero d'avoir", "Numero de devis",
    "Numero de commande", "Numero export", "Invoice number", "Numero de bon",
    # En-tete de tableau nu ("NUMERO"), avant le tres generique "Reference".
    "Numero", "No de facture", "N de facture", "Facture no",
    "Reference",
)
_DATE_LABELS = (
    "Date de facture", "Date de paiement", "Date d'encaissement",
    "Date de reglement", "Date de l'avis", "Invoice date",
    "Date",
)
_DUE_LABELS = (
    "Date limite de paiement", "Echeance", "Date d'echeance", "Due date",
)

# Le montant total ne porte pas le meme libelle selon le document.
# Le libelle "Total" seul est volontairement absent : sur un releve
# bancaire il attraperait "Total credits" et inventerait un TTC.
_TTC_LABELS = {
    "facture_import": ("Total CIF", "Total amount"),
    "facture_export": ("Total export",),
    "avis_penalite": ("Total a payer", "Net a payer"),
    "recu_paiement": ("Montant paye",),
}
_DEFAULT_TTC_LABELS = ("Total TTC", "Total toutes taxes", "Net a payer")

# Tous les libelles sous lesquels un TTC ou un HT peut apparaitre. Sert
# UNIQUEMENT a reperer deux valeurs contradictoires : si deux de ces
# libelles annoncent deux montants differents, aucun choix automatique
# n'est legitime.
_AMBIGUITY_TTC_LABELS = (
    "Total TTC", "Total toutes taxes", "Net a payer", "Montant TTC",
    "Total a payer", "Total du", "Total general", "Total amount", "Amount due",
    "Grand total",
)
_AMBIGUITY_HT_LABELS = (
    "Total HT", "Total hors taxes", "Montant HT", "Sous-total", "Sous total",
    "Subtotal", "Total net", "Net commercial",
)

# Une liste de libelles ne suffit pas. Le document reel qui a echappe au
# controle annoncait "MONTANT A PAYER", synonyme absent de la liste : la
# facture est partie en comptabilite avec 2 400 alors que le document en
# annoncait aussi 2 600. On reconnait donc un libelle de TOTAL FINAL par sa
# FORME, pas par son appartenance a une liste fermee.
_TOTAL_TTC_RE = re.compile(
    r"\b("
    r"TTC"
    r"|A PAYER"
    r"|TOUTES TAXES"
    r"|TOTAL GENERAL"
    r"|TOTAL DU"
    r"|MONTANT DU"
    r"|GRAND TOTAL"
    r"|AMOUNT DUE"
    r"|BALANCE DUE"
    r"|TOTAL AMOUNT"
    r")\b"
)
_TOTAL_HT_RE = re.compile(
    r"\b(HORS TAXES|TOTAL HT|MONTANT HT|SOUS-?TOTAL|SUBTOTAL|NET COMMERCIAL)\b"
)


def labelled_amounts(lines: list[Line], pattern: re.Pattern) -> list[Decimal]:
    """Montants dont le LIBELLE correspond au motif, valeur sur la meme
    ligne ou sur la ligne de montant qui suit.

    Sert uniquement a detecter une contradiction : deux libelles de total
    final annoncant deux valeurs differentes. Aucun de ces montants n'est
    retenu comme valeur comptable.
    """
    found: list[Decimal] = []
    for i, line in enumerate(lines):
        label = line.norm
        if not label or not pattern.search(label):
            continue
        # Une ligne de detail ("TVA 20 %") n'est pas un total final.
        parsed = parse_money(label) if re.search(r"\d", label) else None
        if parsed is None:
            for nxt in lines[i + 1:]:
                if not nxt.text.strip():
                    continue
                parsed = parse_money(nxt.text) if is_amount_line(nxt) else None
                break
        if parsed and parsed[0] not in found:
            found.append(parsed[0])
    return found

# Valeurs qui, dans un champ "facture d'origine", signifient explicitement
# QU'IL N'Y EN A PAS. Les retenir comme reference revenait a croire l'avoir
# rattache a une facture nommee "NON".
_NO_REFERENCE = frozenset({
    "NON", "AUCUNE", "AUCUN", "NEANT", "N/A", "NA", "ND", "-", "--",
    "INCONNUE", "INCONNU", "NONE", "UNKNOWN", "SANS",
})


def plausible_reference(value: str) -> bool:
    """Une reference de document contient au moins un chiffre et n'est pas
    un mot signifiant l'absence."""
    candidate = normalize(value).strip(" .:;,")
    if not candidate or candidate in _NO_REFERENCE:
        return False
    if len(candidate) < 3:
        return False
    return bool(re.search(r"\d", candidate))
_HT_LABELS = {
    "facture_import": ("Goods value", "Total HT", "Total hors taxes"),
    "facture_export": ("Total hors taxes", "Total HT"),
}


def _first_amount(lines: list[Line], labels: tuple[str, ...]) -> Amount | None:
    for label in labels:
        found = amount_after(lines, label)
        if found is not None:
            return found
    return None


def _first_value(lines: list[Line], labels: tuple[str, ...]) -> tuple[str, int] | None:
    for label in labels:
        found = value_after(lines, label)
        if found is not None:
            return found
    return None


def extract_document(
    pages: list[str], *, company: str = "X BLASTE", text_source: str = "native",
    cells: list["Cell"] | None = None,
) -> ExtractedDocument:
    """Classe puis extrait un document. Ne devine jamais un montant absent."""
    joined = "\n".join(pages)
    if not joined.strip():
        raise DocumentExtractError(
            "Aucune couche texte exploitable dans ce document, meme apres OCR."
        )
    classification = classify(joined, company=company)
    lines = build_lines(pages)
    doc = ExtractedDocument(
        classification=classification, text_source=text_source, pages=len(pages),
        raw_text=joined,
    )
    kind = classification.doc_type

    # --- identite -------------------------------------------------------
    # Un numero doit RESSEMBLER a une reference. Sans ce controle, le
    # libelle generique "Reference" attrapait "Reference de paiement :
    # F2026-1101" et, le libelle ne faisant qu'un mot, il restait "de
    # paiement : F2026-1101" : la facture etait comptabilisee sous le
    # numero "de". On essaie donc les libelles dans l'ordre et on retient
    # le premier candidat exploitable.
    doc.numero = None
    for label in _NUMBER_LABELS:
        found = value_after(lines, label)
        if not found:
            continue
        candidate = (found[0].split() or [""])[0].strip(" .:;,")
        if plausible_reference(candidate):
            doc.numero = candidate
            break
    found_date = _first_value(lines, _DATE_LABELS)
    doc.date_document = parse_date(found_date[0]) if found_date else None
    due = _first_value(lines, _DUE_LABELS)
    doc.date_echeance = parse_date(due[0]) if due else None

    # --- parties --------------------------------------------------------
    doc.emetteur, doc.emetteur_ice, _ = party(
        lines, ("Fournisseur", "Emetteur", "Exporter", "Organisme", "Payeur", "Banque")
    )
    doc.destinataire, doc.destinataire_ice, _ = party(
        lines, ("Client", "Acheteur", "Importer", "Beneficiaire", "Titulaire",
                "Destinataire", "Bill to", "Sold to")
    )
    doc.emetteur = clean_party_name(doc.emetteur)
    doc.destinataire = clean_party_name(doc.destinataire)
    if kind in ("bon_commande",):
        # Sur un bon de commande, l'acheteur est en tete et le fournisseur
        # apres : les roles sont inverses par rapport a une facture.
        doc.emetteur, doc.emetteur_ice, _ = party(lines, ("Fournisseur",))
        doc.destinataire, doc.destinataire_ice, _ = party(lines, ("Acheteur", "Client"))
        doc.emetteur = clean_party_name(doc.emetteur)
        doc.destinataire = clean_party_name(doc.destinataire)

    # --- montants -------------------------------------------------------
    if kind != BANK_STATEMENT:
        # Un releve n'a ni HT ni TTC : ses propres totaux sont controles
        # separement par check_bank_totals.
        doc.montant_ht = _first_amount(
            lines, _HT_LABELS.get(kind, ("Total HT", "Total hors taxes"))
        )
        doc.montant_ttc = _first_amount(lines, _TTC_LABELS.get(kind, _DEFAULT_TTC_LABELS))
    if kind == PAYMENT_RECEIPT:
        doc.montant_paye = _first_amount(
            lines, ("Montant paye", "Montant regle", "Montant recu",
                    "Montant encaisse", "Total paye")
        )
        if doc.montant_paye is None:
            # Recu reel sans libelle de montant : si le document ne porte
            # qu'UNE seule valeur monetaire distincte (lignes qui ne sont
            # QUE des montants, dates exclues), elle est le montant paye
            # sans ambiguite possible. Deux valeurs differentes = aucun
            # choix automatique, le champ reste manquant.
            candidats: list[Amount] = []
            for ligne in lines:
                brut = ligne.text.strip()
                if not brut or not _PURE_AMOUNT_RE.match(brut):
                    continue
                if parse_date(brut) is not None:
                    continue
                lu = parse_money(brut)
                if lu is None:
                    continue
                candidats.append(Amount(
                    value=lu[0], currency=lu[1], label="montant unique du recu",
                    page=ligne.page,
                ))
            if len({c.value for c in candidats}) == 1:
                doc.montant_paye = candidats[0]
        doc.montant_ttc = doc.montant_paye
        if doc.destinataire is None:
            # "Recu de : X" designe le payeur. Le libelle n'est retenu
            # qu'avec son deux-points : sans lui, le TITRE "RECU DE
            # PAIEMENT" fournirait un payeur nomme "PAIEMENT".
            for line in lines:
                if line.norm.startswith("RECU DE :") or line.norm.startswith("RECU DE:"):
                    doc.destinataire = clean_party_name(
                        strip_label(line.text, "Recu de")
                    )
                    break
        if doc.numero is None:
            # Le numero d'un recu est souvent imprime en tete, seul sur sa
            # ligne, sans libelle ("REC-2026-001" sous le titre). On ne le
            # devine pas : on n'accepte qu'une ligne d'en-tete qui EST une
            # reference plausible a elle seule, jamais une date ni un
            # montant.
            for header in lines[:8]:
                token = header.text.strip()
                if (" " not in token and plausible_reference(token)
                        and parse_date(token) is None
                        and not _PURE_AMOUNT_RE.match(token)):
                    doc.numero = token
                    break
    if kind in (IMPORT_INVOICE, EXPORT_INVOICE):
        doc.frais_annexes = _first_amount(
            lines, ("Freight and insurance", "Fret et assurance", "Frais annexes")
        )

    if kind != BANK_STATEMENT:
        doc.taux_tva, doc.montant_tva = extract_vat(lines)

    doc.devise = next(
        (a.currency for a in doc.amounts if a.currency),
        (parse_money(_first_value(lines, ("Devise", "Currency"))[0])[1]
         if False else ""),
    )
    if not doc.devise:
        explicit = _first_value(lines, ("Devise", "Currency"))
        if explicit:
            candidate = normalize(explicit[0]).split()[0] if explicit[0] else ""
            if candidate in CURRENCIES:
                doc.devise = "MAD" if candidate in ("DH", "DHS") else candidate

    # --- divers ---------------------------------------------------------
    statut = _first_value(lines, ("Statut",))
    doc.statut = statut[0] if statut else None
    mode = _first_value(lines, ("Mode de paiement", "Mode de reglement", "Mode"))
    doc.mode_paiement = mode[0] if mode else None
    motif = _first_value(lines, ("Motif", "Objet", "Nature"))
    doc.motif = motif[0] if motif else ""
    # "Document d'origine" (avoirs) et "Facture concernee" (recus) sont les
    # libelles REELS des pieces : leur absence de cette liste envoyait tout
    # avoir legitime en quarantaine "sans facture d'origine identifiable".
    linked = _first_value(lines, (
        "Facture reglee", "Facture d'origine", "Facture liee",
        "Document d'origine", "Reference d'origine", "Document lie",
        "Facture concernee",
    ))
    if linked:
        candidate = (linked[0].split() or [""])[0]
        doc.facture_liee_brute = linked[0]
        doc.facture_liee = candidate if plausible_reference(candidate) else None

    incoterm = _first_value(lines, ("Incoterm",))
    doc.incoterm = incoterm[0] if incoterm else ""
    origin = _first_value(lines, ("Country of origin", "Pays d'origine"))
    doc.pays_origine = origin[0] if origin else ""
    destination = _first_value(lines, ("Pays de destination", "Country of destination"))
    doc.pays_destination = destination[0] if destination else ""

    # --- contenu --------------------------------------------------------
    if kind == BANK_STATEMENT:
        # La position reelle des colonnes prime : elle DIT le sens de
        # l'operation. La deduction par variation de solde ne sert que si le
        # releve ne porte pas d'en-tetes Debit/Credit exploitables.
        par_colonne, bank_anomalies = (
            extract_bank_lines_by_column(cells, doc.devise or "MAD") if cells else ([], [])
        )
        if par_colonne:
            doc.bank_lines = par_colonne
        else:
            doc.bank_lines, bank_anomalies = extract_bank_lines(lines, doc.devise or "MAD")
        doc.anomalies.extend(bank_anomalies)
        doc.anomalies.extend(check_bank_totals(doc, lines))
        period = _first_value(lines, ("Periode",))
        if period:
            doc.date_document = parse_date(period[0]) or doc.date_document
    else:
        doc.lignes = extract_detail_lines(lines)

    # --- signature des avoirs -------------------------------------------
    if kind in SIGNED_NEGATIVE_TYPES:
        for attr in ("montant_ht", "montant_tva", "montant_ttc"):
            amount = getattr(doc, attr)
            if amount is not None:
                setattr(doc, attr, amount.signed(negative=True))

    # --- controles deterministes ----------------------------------------
    doc.missing = [
        f for f in REQUIRED_FIELDS.get(kind, ()) if getattr(doc, f, None) is None
    ]
    # Ambiguite de montant : un document reel ne repete pas le meme libelle,
    # il propose DEUX libelles differents ("Total TTC" et "Net a payer") avec
    # deux valeurs differentes. Ne regarder qu'une etiquette laissait passer
    # exactement ce cas : la facture partait en comptabilite avec l'un des
    # deux montants, choisi par l'ordre des lignes.
    for attr, labels, motif in (
        ("montant_ht", _AMBIGUITY_HT_LABELS + _HT_LABELS.get(kind, ()), _TOTAL_HT_RE),
        ("montant_ttc", _AMBIGUITY_TTC_LABELS + _TTC_LABELS.get(kind, ()), _TOTAL_TTC_RE),
    ):
        proposees: list[Decimal] = []
        for label in labels:
            proposees.extend(all_amounts_for(lines, label))
        if kind != BANK_STATEMENT:
            # Le releve bancaire porte ses propres totaux ("Total credits"),
            # controles ailleurs : le motif ne s'y applique pas.
            proposees.extend(labelled_amounts(lines, motif))
        if len({abs(v) for v in proposees}) > 1:
            doc.ambigus.append(attr)

    if doc.montant_ht and doc.montant_tva and doc.montant_ttc:
        expected = doc.montant_ht.value + doc.montant_tva.value
        if expected != doc.montant_ttc.value:
            doc.anomalies.append(
                f"HT + TVA = {expected} mais TTC indique = {doc.montant_ttc.value} "
                f"(ecart {abs(expected - doc.montant_ttc.value)})"
            )
    if doc.lignes and doc.montant_ht is not None:
        total_lignes = sum(
            (abs(l.total) for l in doc.lignes if l.total is not None), Decimal("0")
        )
        if total_lignes != abs(doc.montant_ht.value):
            doc.anomalies.append(
                f"somme des lignes {total_lignes} != total HT {abs(doc.montant_ht.value)}"
            )
    if doc.date_document and doc.date_echeance and doc.date_echeance < doc.date_document:
        doc.anomalies.append("la date d'echeance precede la date du document")

    currencies = {a.currency for a in doc.amounts if a.currency}
    # Balayage complet : les lignes de detail et les prix unitaires ne
    # portent pas de libelle de total et echappaient a doc.amounts.
    for line in lines:
        for code in _MONEY_CODE_RE.findall(line.text):
            currencies.add(code.upper())
    doc.devises_detectees = sorted(
        {"MAD" if c in ("DH", "DHS") else c for c in currencies}
    )
    if len(currencies) > 1:
        doc.anomalies.append(f"plusieurs devises dans le meme document : {sorted(currencies)}")

    return doc


def extract_from_pdf_bytes(
    data: bytes, *, company: str = "X BLASTE", ocr: bool = True
) -> ExtractedDocument:
    """Lit un PDF : couche texte native, puis OCR si elle est insuffisante."""
    pages = read_pdf_pages(data)
    source = "native"
    cells = read_pdf_cells(data)
    if ocr and not _has_usable_text(pages):
        ocr_pages = ocr_pdf_pages(data)
        if _has_usable_text(ocr_pages):
            pages, source, cells = ocr_pages, "ocr", []
    return extract_document(pages, company=company, text_source=source, cells=cells)


def read_image_text(data: bytes) -> str:
    """OCR d'une image (facture photographiee), avec garde-fous.

    Trois refus, tous par une erreur CLAIRE et non par un abandon silencieux,
    pour que l'appelant inscrive une ligne tracable en quarantaine :
      - une image trop grande (bombe de decompression) est refusee AVANT
        d'etre ouverte en grand ;
      - une image corrompue ou illisible leve ;
      - un moteur OCR absent ou en echec leve.
    """
    try:
        from PIL import Image  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependance manquante
        raise DocumentExtractError(f"Dependance Pillow manquante: {exc}") from exc
    import io

    from app.attachments import MAX_IMAGE_PIXELS

    # Plafonner Pillow AVANT tout decodage complet : une image piege ne doit
    # jamais etre ouverte en grand.
    # Pillow leve de lui-meme une DecompressionBombError au-dela de 2x ce
    # plafond ; notre controle explicite couvre la zone entre le plafond et
    # ce double. Les deux menent au MEME message clair (trop volumineuse),
    # jamais au message generique d'image corrompue.
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            if width * height > MAX_IMAGE_PIXELS:
                raise DocumentExtractError(
                    f"image trop volumineuse ({width}x{height} pixels, "
                    f"plafond {MAX_IMAGE_PIXELS} pixels)"
                )
            img.load()
            # Une photo mobile porte son orientation dans les metadonnees
            # EXIF : sans cette transposition, le texte est lu de cote.
            try:
                from PIL import ImageOps
                img = ImageOps.exif_transpose(img)
            except Exception:  # noqa: BLE001 - EXIF corrompu : image telle quelle
                pass
            frame = img.convert("RGB")
    except DocumentExtractError:
        raise
    except Image.DecompressionBombError as exc:
        raise DocumentExtractError(
            f"image trop volumineuse (bombe de decompression, plafond "
            f"{MAX_IMAGE_PIXELS} pixels)"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - image corrompue / illisible
        raise DocumentExtractError("image illisible ou corrompue.") from exc

    try:
        import pytesseract  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependance manquante
        raise DocumentExtractError(f"Dependance pytesseract manquante: {exc}") from exc

    # Redressement d'orientation : l'OSD de tesseract detecte une image
    # tournee de 90/180/270 degres. Detection bornee et non bloquante - un
    # OSD en echec (image trop pauvre) laisse l'image telle quelle.
    try:
        osd = pytesseract.image_to_osd(frame)
        m = re.search(r"Rotate:\s*(\d+)", osd or "")
        angle = int(m.group(1)) if m else 0
        if angle in (90, 180, 270):
            frame = frame.rotate(-angle, expand=True)
    except Exception:  # noqa: BLE001 - OSD indisponible : pas de rotation
        pass

    # Passes d'OCR BORNEES (au plus trois variantes deterministes), chacune
    # tracable : image telle quelle, image nettoyee (gris, contraste,
    # debruitage, nettete, remise a l'echelle ~300 DPI), image binarisee
    # pour les photos sombres. On garde la MEILLEURE lecture - jamais une
    # fusion - selon un score de texte exploitable. Aucun seuil de decision
    # comptable n'est touche : seule la QUALITE de la lecture s'ameliore.
    def _score(texte: str) -> int:
        return sum(
            len(mot) for mot in re.findall(r"[0-9A-Za-zÀ-ÿ]{2,}", texte or "")
        )

    def _ocr(image) -> str:
        last_error: Exception | None = None
        # On vise le francais ; un serveur sans le pack `fra` retombe sur
        # `eng`, qui lit tout aussi bien les caracteres latins. Seul un
        # echec des DEUX est un vrai echec OCR.
        for lang in ("fra+eng", "eng"):
            try:
                return pytesseract.image_to_string(image, lang=lang)
            except Exception as exc:  # noqa: BLE001 - langue absente / moteur KO
                last_error = exc
        raise DocumentExtractError("OCR de l'image en echec.") from last_error

    variantes = [frame]
    try:
        from PIL import ImageFilter, ImageOps
        gris = ImageOps.grayscale(frame)
        gris = ImageOps.autocontrast(gris, cutoff=1)
        gris = gris.filter(ImageFilter.MedianFilter(3))
        gris = gris.filter(ImageFilter.SHARPEN)
        if min(gris.size) < 1500:
            facteur = min(2, int(1500 / max(1, min(gris.size))) + 1)
            cible = (gris.size[0] * facteur, gris.size[1] * facteur)
            if cible[0] * cible[1] <= MAX_IMAGE_PIXELS:
                gris = gris.resize(cible, Image.LANCZOS)
        variantes.append(gris)
        variantes.append(gris.point(lambda p: 255 if p > 140 else 0))
    except Exception:  # noqa: BLE001 - pretraitement KO : l'original suffit
        pass

    meilleur_texte, meilleur_score = "", -1
    derniere_erreur: DocumentExtractError | None = None
    for variante in variantes[:3]:
        try:
            texte = _ocr(variante)
        except DocumentExtractError as exc:
            derniere_erreur = exc
            continue
        note = _score(texte)
        if note > meilleur_score:
            meilleur_texte, meilleur_score = texte, note
    if meilleur_score < 0 and derniere_erreur is not None:
        raise derniere_erreur
    return meilleur_texte


def extract_from_image_bytes(
    data: bytes, *, company: str = "X BLASTE", ocr: bool = True
) -> ExtractedDocument:
    """Lit une facture photographiee (PNG/JPEG) par OCR, comme un PDF scanne.

    Le texte OCR passe ENSUITE par le meme moteur de classification et
    d'extraction que les PDF : memes controles comptables, meme seuil de
    confiance, meme politique de quarantaine. Une image sans texte
    exploitable leve `DocumentExtractError` et part en validation humaine
    avec une raison tracable.
    """
    text = read_image_text(data)
    return extract_document([text], company=company, text_source="ocr")


# Un PDF scanne renvoie quelques caracteres parasites : on exige un minimum
# de texte reel avant de considerer la couche native comme exploitable.
_MIN_USABLE_CHARS = 120


def _has_usable_text(pages: list[str]) -> bool:
    return sum(len((p or "").strip()) for p in pages) >= _MIN_USABLE_CHARS


def read_pdf_pages(data: bytes) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependance manquante
        raise DocumentExtractError(f"Dependance pypdf manquante: {exc}") from exc
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
        return [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - PDF corrompu / chiffre
        raise DocumentExtractError("PDF illisible ou corrompu.") from exc


def ocr_pdf_pages(data: bytes) -> list[str]:
    """OCR de secours. Absence d'outil OCR = liste vide, jamais une exception :
    le document partira simplement en validation humaine."""
    try:
        import pdf2image  # type: ignore
        import pytesseract  # type: ignore
    except ImportError:
        return []
    try:
        images = pdf2image.convert_from_bytes(data, dpi=300)
        return [pytesseract.image_to_string(img, lang="fra+eng") for img in images]
    except Exception:  # noqa: BLE001 - OCR indisponible ou en echec
        return []
