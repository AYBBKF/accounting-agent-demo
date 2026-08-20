"""Client de synchronisation Google Sheets (demo).

Utilise gspread + un compte de service Google (aucun secret dans le code
ni dans l'image : la cle JSON du compte de service et l'ID du classeur
viennent exclusivement de l'environnement / .env).

La synchronisation est best-effort et idempotente : chaque ligne est
identifiee par une cle stable (colonne ID en premiere colonne de chaque
onglet). Relancer une synchronisation ne cree jamais de doublon : une
ligne dont l'ID existe deja est mise a jour en place, sinon elle est
ajoutee a la fin.

Aucun appel a OpenAI n'est effectue ici : la synchronisation Sheets ne
consomme jamais de tokens OpenAI (uniquement l'extraction de documents
le fait, ailleurs dans le code).
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("demo_bot.sheets")


class SheetsSyncError(RuntimeError):
    pass


class SheetsClient:
    """Enveloppe fine autour de gspread, initialisee paresseusement.

    Si aucun compte de service n'est configure, `is_configured` est False
    et toute tentative de synchronisation echoue proprement avec un
    message clair (jamais de crash du bot, jamais de secret dans les logs).
    """

    def __init__(
        self,
        service_account_json: str,
        service_account_file: str,
        spreadsheet_id: str,
    ) -> None:
        self._spreadsheet_id = spreadsheet_id
        self._service_account_json = service_account_json
        self._service_account_file = service_account_file
        self._gc = None
        self._sh = None

    @property
    def is_configured(self) -> bool:
        return bool(self._spreadsheet_id) and bool(
            self._service_account_json or self._service_account_file
        )

    def sheet_url(self) -> str | None:
        if not self._spreadsheet_id:
            return None
        return f"https://docs.google.com/spreadsheets/d/{self._spreadsheet_id}/edit"

    def _ensure_client(self):
        if self._gc is not None:
            return self._gc
        if not self.is_configured:
            raise SheetsSyncError(
                "Synchronisation Google Sheets non configuree "
                "(GOOGLE_SERVICE_ACCOUNT_JSON/_FILE ou GOOGLE_SHEET_ID manquants)."
            )
        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError as exc:  # pragma: no cover - dependance manquante
            raise SheetsSyncError(f"Dependance Google Sheets manquante: {exc}") from exc

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ]
        try:
            if self._service_account_json:
                info = json.loads(self._service_account_json)
                creds = Credentials.from_service_account_info(info, scopes=scopes)
            else:
                creds = Credentials.from_service_account_file(
                    self._service_account_file, scopes=scopes
                )
            self._gc = gspread.authorize(creds)
        except Exception as exc:  # noqa: BLE001 - on ne veut jamais crasher le bot
            # Ne jamais logger le contenu de la cle de service.
            raise SheetsSyncError("Authentification Google Sheets echouee.") from exc
        return self._gc

    def _ensure_spreadsheet(self):
        if self._sh is not None:
            return self._sh
        gc = self._ensure_client()
        try:
            self._sh = gc.open_by_key(self._spreadsheet_id)
        except Exception as exc:  # noqa: BLE001
            raise SheetsSyncError("Impossible d'ouvrir le classeur Google Sheets.") from exc
        return self._sh

    def upsert_rows(
        self, sheet_name: str, headers: list[str], rows: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Met a jour ou ajoute des lignes dans un onglet, par ID stable
        (rows[i]["id"] doit correspondre a la colonne `headers[0]`, ex. "ID").

        Retourne {"updated": n, "appended": n} pour le rapport de sync.
        """
        sh = self._ensure_spreadsheet()
        try:
            ws = sh.worksheet(sheet_name)
        except Exception as exc:  # noqa: BLE001
            raise SheetsSyncError(f"Onglet '{sheet_name}' introuvable.") from exc

        try:
            existing_ids = ws.col_values(1)  # colonne A = ID
        except Exception as exc:  # noqa: BLE001
            raise SheetsSyncError(f"Lecture de l'onglet '{sheet_name}' impossible.") from exc

        id_to_row_number = {
            value: idx + 1 for idx, value in enumerate(existing_ids) if idx > 0 and value
        }

        updated = 0
        to_append = []
        for row in rows:
            row_id = str(row.get("id", ""))
            if not row_id:
                continue
            values = [row.get(key, "") for key in headers]
            if row_id in id_to_row_number:
                row_number = id_to_row_number[row_id]
                try:
                    ws.update(
                        f"A{row_number}:{chr(64 + len(headers))}{row_number}",
                        [values],
                    )
                    updated += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Echec mise a jour ligne %s de %s: %s", row_id, sheet_name, exc)
            else:
                to_append.append(values)

        if to_append:
            try:
                ws.append_rows(to_append, value_input_option="USER_ENTERED")
            except Exception as exc:  # noqa: BLE001
                raise SheetsSyncError(f"Echec ajout de lignes dans '{sheet_name}'.") from exc

        return {"updated": updated, "appended": len(to_append)}

    def update_cell(self, sheet_name: str, a1_range: str, value: Any) -> None:
        sh = self._ensure_spreadsheet()
        ws = sh.worksheet(sheet_name)
        ws.update(a1_range, [[value]])
