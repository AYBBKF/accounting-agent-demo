"""Faux classeur, faux Drive, faux Calendar - en memoire.

Reproduit fidelement le comportement observe sur le classeur reel du client
(onglets, plages A1, lecture/ecriture) pour que les tests d'integration
prouvent ce qui est ECRIT, et surtout ce qui ne l'est pas. Aucun appel
reseau.
"""
from __future__ import annotations

import re
from typing import Any

# Onglets reels du classeur X BLASTE, avec leurs en-tetes constates.
PURCHASE_HEADERS = [
    "ID", "Date", "Numéro facture", "ID Fournisseur", "Fournisseur", "Description",
    "Montant HT (facture)", "Taux TVA (%)", "Montant TVA (facture)",
    "Montant TTC (facture)", "Montant TTC théorique (formule)", "Écart TTC (formule)",
    "Doublon numéro? (formule)", "Échéance", "Montant payé", "Statut",
    "Jours de retard (formule)",
]
SALES_HEADERS = [
    h.replace("ID Fournisseur", "ID Client").replace("Fournisseur", "Client")
    if h in ("ID Fournisseur", "Fournisseur") else h
    for h in PURCHASE_HEADERS
]
BANK_HEADERS = [
    "ID", "Compte", "Date opération", "Date valeur", "Libellé", "Référence",
    "Débit", "Crédit", "Solde (relevé)", "Tiers", "Catégorie", "Facture liée",
    "Statut rapprochement",
]
PARTY_HEADERS = [
    "ID", "Raison sociale", "ICE", "Téléphone", "Email", "Adresse",
    "Délai de paiement (jours)",
]
LOG_HEADERS = [
    "Date/Heure sync", "Type d'enregistrement", "ID stable",
    "Action (Créé/Mis à jour)", "Statut", "Détail",
]

DEMO_SUPPLIERS = [
    ["FRS-001", "Fournitures Atlas SARL (DEMO)", "DEMO-ICE-200341", "", "", "", 30],
    ["FRS-002", "Papeterie Zellige (DEMO)", "DEMO-ICE-200342", "", "", "", 30],
    ["FRS-003", "Transport Sindibad (DEMO)", "DEMO-ICE-200343", "", "", "", 15],
    ["FRS-004", "Cyber Cafe Medina Services (DEMO)", "DEMO-ICE-200344", "", "", "", 30],
    ["FRS-005", "Imprimerie Argan (DEMO)", "DEMO-ICE-200345", "", "", "", 30],
    ["FRS-006", "ATLAS BUREAU SARL", "002345678000043", "", "", "", 30],
]
DEMO_CLIENTS = [
    ["CLI-001", "Atlas Textile SARL (DEMO)", "DEMO-ICE-100234", "", "", "", 30],
    ["CLI-002", "Riad Marrakech Hotels (DEMO)", "DEMO-ICE-100235", "", "", "", 45],
]
DEMO_PURCHASES = [
    [f"FA-2026-{i:03d}", 46200 + i, f"FAC-ACH-2026-{i:03d}", f"FRS-{(i % 5) + 1:03d}",
     "Fournisseur DEMO", "Achat DEMO", 1000.0, 20.0, 200.0, 1200.0, "", "", "",
     46230 + i, 1200.0, "Payee", ""]
    for i in range(1, 13)
]
DEMO_SALES = [
    [f"FV-2026-{i:03d}", 46200 + i, f"FAC-VTE-2026-{i:03d}", f"CLI-00{(i % 2) + 1}",
     "Client DEMO", "Vente DEMO", 2000.0, 20.0, 400.0, 2400.0, "", "", "",
     46230 + i, 2400.0, "Payee", ""]
    for i in range(1, 13)
]

_RANGE_RE = re.compile(r"^'?([^'!]+)'?!([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$")


def column_index(letters: str) -> int:
    value = 0
    for char in letters:
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


class FakeWorkbook:
    """Classeur en memoire + Drive + Calendar, avec journal des appels."""

    def __init__(self, *, with_lines_tab: bool = False) -> None:
        self.tabs: dict[str, list[list[Any]]] = {
            "00_DASHBOARD": [["Indicateur", "Valeur"]],
            "02_CLIENTS": [PARTY_HEADERS] + [list(r) for r in DEMO_CLIENTS],
            "03_FOURNISSEURS": [PARTY_HEADERS] + [list(r) for r in DEMO_SUPPLIERS],
            "04_FACTURES_VENTES": [SALES_HEADERS] + [list(r) for r in DEMO_SALES],
            "05_FACTURES_ACHATS": [PURCHASE_HEADERS] + [list(r) for r in DEMO_PURCHASES],
            "06_RELEVE_BANCAIRE": [BANK_HEADERS],
            "14_IMPORTS_LOG": [LOG_HEADERS],
        }
        if with_lines_tab:
            self.tabs["16_LIGNES_FACTURES"] = [["ID facture"]]
        self.calls: list[tuple[str, dict]] = []
        self.formats: list[dict] = []
        self.validations: list[dict] = []
        self.folders: dict[str, str] = {}
        self.uploads: list[dict] = []
        self.events: list[dict] = []
        self.drive_fails = False
        self._next_folder = 1

    # -- introspection pour les assertions --------------------------------

    @property
    def slugs(self) -> list[str]:
        return [s for s, _ in self.calls]

    def writes_to(self, tab: str) -> list[dict]:
        return [
            a for s, a in self.calls
            if s == "GOOGLESHEETS_VALUES_UPDATE" and a["range"].startswith(tab)
        ]

    def rows(self, tab: str) -> list[list[Any]]:
        return self.tabs.get(tab, [])[1:]

    def row(self, tab: str, index: int) -> list[Any]:
        """Ligne 1-based telle que la verrait le client dans son classeur."""
        table = self.tabs.get(tab, [])
        return table[index - 1] if 0 < index <= len(table) else []

    # -- moteur -----------------------------------------------------------

    def _ensure_size(self, tab: str, row: int, col: int) -> None:
        table = self.tabs.setdefault(tab, [])
        while len(table) < row:
            table.append([])
        line = table[row - 1]
        while len(line) <= col:
            line.append("")

    def execute(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((slug, arguments))

        if slug == "GOOGLESHEETS_GET_SPREADSHEET_INFO":
            if arguments.get("ranges"):
                return {"sheets": [{"data": [{"rowData": [{"values": [
                    {"effectiveFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}}
                ]}]}]}]}
            return {"sheets": [
                {"properties": {"title": title, "sheetId": index}}
                for index, title in enumerate(self.tabs)
            ]}

        if slug == "GOOGLESHEETS_BATCH_GET":
            a1 = (arguments.get("ranges") or [""])[0]
            return {"valueRanges": [{"values": self._read_range(a1)}]}

        if slug == "GOOGLESHEETS_VALUES_UPDATE":
            self._write_range(arguments["range"], arguments["values"])
            return {"updatedRows": len(arguments["values"])}

        if slug == "GOOGLESHEETS_ADD_SHEET":
            title = arguments["title"]
            self.tabs.setdefault(title, [])
            return {"replies": [{"addSheet": {"properties": {"title": title}}}]}

        if slug == "GOOGLESHEETS_FORMAT_CELL":
            self.formats.append(arguments)
            return {}

        if slug == "GOOGLESHEETS_SET_DATA_VALIDATION_RULE":
            self.validations.append(arguments)
            return {}

        if slug == "GOOGLEDRIVE_FIND_FOLDER":
            name = arguments["folder_name"]
            if name in self.folders:
                return {"files": [{"id": self.folders[name]}]}
            return {"files": []}

        if slug == "GOOGLEDRIVE_CREATE_FOLDER":
            name = arguments["folder_name"]
            folder_id = f"folder-{self._next_folder}"
            self._next_folder += 1
            self.folders[name] = folder_id
            return {"id": folder_id}

        if slug == "GOOGLEDRIVE_UPLOAD_FROM_URL":
            if self.drive_fails:
                raise RuntimeError("Drive indisponible")
            self.uploads.append(arguments)
            return {"id": f"file-{len(self.uploads)}"}

        if slug == "GOOGLECALENDAR_CREATE_EVENT":
            self.events.append(arguments)
            return {"id": f"event-{len(self.events)}"}

        return {}

    def _read_range(self, a1: str) -> list[list[Any]]:
        m = _RANGE_RE.match(a1)
        if not m:
            return []
        tab, col1, row1, col2, row2 = m.groups()
        table = self.tabs.get(tab, [])
        start_row, end_row = int(row1), int(row2 or row1)
        start_col = column_index(col1)
        end_col = column_index(col2 or col1)
        out: list[list[Any]] = []
        for index in range(start_row, min(end_row, len(table)) + 1):
            line = table[index - 1]
            out.append([line[c] if c < len(line) else "" for c in range(start_col, end_col + 1)])
        while out and all(cell == "" for cell in out[-1]):
            out.pop()
        return out

    def _write_range(self, a1: str, values: list[list[Any]]) -> None:
        m = _RANGE_RE.match(a1)
        if not m:
            raise AssertionError(f"plage invalide : {a1}")
        tab, col1, row1, _, _ = m.groups()
        start_row, start_col = int(row1), column_index(col1)
        for offset, line in enumerate(values):
            row = start_row + offset
            self._ensure_size(tab, row, start_col + len(line) - 1)
            target = self.tabs[tab][row - 1]
            for index, value in enumerate(line):
                target[start_col + index] = value
