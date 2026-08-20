"""Client de synchronisation Google Sheets (demo) via Composio.

Utilise la connexion Google Sheets deja active dans Composio (OAuth) :
AUCUN compte de service Google, AUCUNE cle JSON. L'authentification
Composio est scopee par COMPOSIO_API_KEY et un identifiant de compte
(COMPOSIO_USER_ID ou COMPOSIO_CONNECTED_ACCOUNT_ID). L'ID du classeur
cible vient de GOOGLE_SHEET_ID.

Appelle directement l'API REST Composio (POST
https://backend.composio.dev/api/v3.1/tools/execute/{tool_slug},
header `x-api-key`) via `httpx`, plutot que le SDK `composio` officiel :
ce dernier impose `pydantic>=2.10`, incompatible avec `aiogram==3.15.0`
(qui exige `pydantic<2.10`) dans cette image. L'appel REST reproduit
exactement le comportement du SDK (meme host, meme chemin, meme
enveloppe de requete/reponse) sans ce conflit de dependances.

La synchronisation est best-effort et idempotente : elle delegue a
l'outil Composio GOOGLESHEETS_UPSERT_ROWS, qui met a jour en place toute
ligne dont l'ID (premiere colonne des headers) existe deja et ajoute les
autres a la fin. Relancer une synchronisation ne cree jamais de doublon.

Aucun appel a OpenAI n'est effectue ici : la synchronisation Sheets ne
consomme jamais de tokens OpenAI (uniquement l'extraction de documents
le fait, ailleurs dans le code).

Aucun secret (cle API Composio, identifiants de compte) n'est jamais
journalise ni renvoye dans un message d'erreur.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("demo_bot.sheets")

COMPOSIO_BASE_URL = "https://backend.composio.dev"
COMPOSIO_TOOLS_EXECUTE_PATH = "/api/v3.1/tools/execute/{tool_slug}"
_REQUEST_TIMEOUT_SECONDS = 30.0


class SheetsSyncError(RuntimeError):
    pass


class SheetsClient:
    """Enveloppe fine autour de l'API REST Composio, initialisee paresseusement.

    Si Composio n'est pas configure (cle API, identifiant de compte ou
    ID de classeur manquants), `is_configured` est False et toute
    tentative de synchronisation echoue proprement avec un message
    clair (jamais de crash du bot, jamais de secret dans les logs).
    """

    def __init__(
        self,
        composio_api_key: str,
        composio_user_id: str,
        composio_connected_account_id: str,
        spreadsheet_id: str,
    ) -> None:
        self._composio_api_key = composio_api_key
        self._composio_user_id = composio_user_id
        self._composio_connected_account_id = composio_connected_account_id
        self._spreadsheet_id = spreadsheet_id
        self._client = None

    @property
    def is_configured(self) -> bool:
        has_account_ref = bool(self._composio_user_id or self._composio_connected_account_id)
        return bool(self._composio_api_key) and bool(self._spreadsheet_id) and has_account_ref

    def sheet_url(self) -> str | None:
        if not self._spreadsheet_id:
            return None
        return f"https://docs.google.com/spreadsheets/d/{self._spreadsheet_id}/edit"

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.is_configured:
            raise SheetsSyncError(
                "Synchronisation Google Sheets non configuree "
                "(COMPOSIO_API_KEY / COMPOSIO_USER_ID ou "
                "COMPOSIO_CONNECTED_ACCOUNT_ID / GOOGLE_SHEET_ID manquants)."
            )
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependance manquante
            raise SheetsSyncError(f"Dependance httpx manquante: {exc}") from exc
        self._client = httpx.Client(
            base_url=COMPOSIO_BASE_URL,
            headers={"x-api-key": self._composio_api_key, "Content-Type": "application/json"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        return self._client

    def _execute(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = self._ensure_client()
        body: dict[str, Any] = {"arguments": arguments}
        if self._composio_user_id:
            body["user_id"] = self._composio_user_id
        else:
            body["connected_account_id"] = self._composio_connected_account_id
        path = COMPOSIO_TOOLS_EXECUTE_PATH.format(tool_slug=slug)
        try:
            response = client.post(path, json=body)
            response.raise_for_status()
            result = response.json()
        except Exception as exc:  # noqa: BLE001 - jamais de secret dans le log/l'erreur
            raise SheetsSyncError(f"Appel Composio '{slug}' echoue.") from exc
        if not result.get("successful", False):
            raise SheetsSyncError(
                f"Outil Composio '{slug}' a echoue: {result.get('error') or 'erreur inconnue'}."
            )
        return result.get("data") or {}

    def upsert_rows(
        self, sheet_name: str, headers: list[str], rows: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Met a jour ou ajoute des lignes dans un onglet, par ID stable
        (rows[i]["id"] doit correspondre a la colonne `headers[0]`, ex. "ID").

        Delegue integralement a l'outil Composio GOOGLESHEETS_UPSERT_ROWS
        (cle = premiere colonne des headers), qui gere lui-meme la
        correspondance mise-a-jour / ajout sans jamais creer de doublon.

        Retourne {"updated": n, "appended": n} pour le rapport de sync.
        """
        if not rows:
            return {"updated": 0, "appended": 0}
        key_column = headers[0]
        data_rows = [[row.get(key, "") for key in headers] for row in rows]
        data = self._execute(
            "GOOGLESHEETS_UPSERT_ROWS",
            {
                "spreadsheetId": self._spreadsheet_id,
                "sheetName": sheet_name,
                "headers": headers,
                "rows": data_rows,
                "keyColumn": key_column,
            },
        )
        return {
            "updated": int(data.get("rowsUpdated", 0)),
            "appended": int(data.get("rowsInserted", 0)),
        }

    def update_cell(self, sheet_name: str, a1_range: str, value: Any) -> None:
        self._execute(
            "GOOGLESHEETS_VALUES_UPDATE",
            {
                "spreadsheet_id": self._spreadsheet_id,
                "range": f"{sheet_name}!{a1_range}",
                "values": [[value]],
                "value_input_option": "USER_ENTERED",
            },
        )
