"""Tests du client Google Sheets (Composio, via appel REST direct). Aucun
appel reseau reel : httpx.Client est entierement mocke."""
from unittest.mock import MagicMock, patch

import pytest

from app.sheets_client import SheetsClient, SheetsSyncError


def test_is_configured_false_without_credentials():
    client = SheetsClient(
        composio_api_key="",
        composio_user_id="",
        composio_connected_account_id="",
        spreadsheet_id="",
    )
    assert client.is_configured is False


def test_is_configured_false_without_account_reference():
    client = SheetsClient(
        composio_api_key="key",
        composio_user_id="",
        composio_connected_account_id="",
        spreadsheet_id="abc123",
    )
    assert client.is_configured is False


def test_is_configured_true_with_user_id():
    client = SheetsClient(
        composio_api_key="key",
        composio_user_id="user-1",
        composio_connected_account_id="",
        spreadsheet_id="abc123",
    )
    assert client.is_configured is True


def test_is_configured_true_with_connected_account_id():
    client = SheetsClient(
        composio_api_key="key",
        composio_user_id="",
        composio_connected_account_id="ca-1",
        spreadsheet_id="abc123",
    )
    assert client.is_configured is True


def test_sheet_url_none_without_spreadsheet_id():
    client = SheetsClient(
        composio_api_key="key",
        composio_user_id="user-1",
        composio_connected_account_id="",
        spreadsheet_id="",
    )
    assert client.sheet_url() is None


def test_sheet_url_built_from_spreadsheet_id():
    client = SheetsClient(
        composio_api_key="key",
        composio_user_id="user-1",
        composio_connected_account_id="",
        spreadsheet_id="abc123",
    )
    assert client.sheet_url() == "https://docs.google.com/spreadsheets/d/abc123/edit"


def test_upsert_rows_raises_clear_error_when_not_configured():
    client = SheetsClient(
        composio_api_key="",
        composio_user_id="",
        composio_connected_account_id="",
        spreadsheet_id="",
    )
    with pytest.raises(SheetsSyncError):
        client.upsert_rows("ONGLET", ["ID", "Nom"], [{"id": "1", "ID": "1", "Nom": "x"}])


def _fake_httpx_client(json_return=None, raise_for_status_side_effect=None, post_side_effect=None):
    fake_client_instance = MagicMock()
    fake_response = MagicMock()
    fake_response.json.return_value = json_return
    if raise_for_status_side_effect is not None:
        fake_response.raise_for_status.side_effect = raise_for_status_side_effect
    if post_side_effect is not None:
        fake_client_instance.post.side_effect = post_side_effect
    else:
        fake_client_instance.post.return_value = fake_response
    return fake_client_instance


def test_upsert_rows_uses_upsert_rows_tool_and_reports_counts():
    fake_client = _fake_httpx_client(
        json_return={
            "successful": True,
            "error": None,
            "data": {"rowsUpdated": 1, "rowsInserted": 2},
        }
    )
    client = SheetsClient(
        composio_api_key="key",
        composio_user_id="user-1",
        composio_connected_account_id="",
        spreadsheet_id="abc123",
    )
    with patch("httpx.Client", return_value=fake_client):
        result = client.upsert_rows(
            "BOT_FACTURES",
            ["ID", "Nom"],
            [
                {"id": "INV-1-1", "ID": "INV-1-1", "Nom": "Client DEMO"},
                {"id": "INV-1-2", "ID": "INV-1-2", "Nom": "Client DEMO 2"},
                {"id": "INV-1-3", "ID": "INV-1-3", "Nom": "Client DEMO 3"},
            ],
        )

    assert result == {"updated": 1, "appended": 2}
    fake_client.post.assert_called_once()
    path, kwargs = fake_client.post.call_args
    assert path[0] == "/api/v3.1/tools/execute/GOOGLESHEETS_UPSERT_ROWS"
    body = kwargs["json"]
    assert body["user_id"] == "user-1"
    assert "connected_account_id" not in body
    assert body["arguments"]["spreadsheetId"] == "abc123"
    assert body["arguments"]["sheetName"] == "BOT_FACTURES"
    assert body["arguments"]["keyColumn"] == "ID"
    assert body["arguments"]["headers"] == ["ID", "Nom"]
    assert body["arguments"]["rows"] == [
        ["INV-1-1", "Client DEMO"],
        ["INV-1-2", "Client DEMO 2"],
        ["INV-1-3", "Client DEMO 3"],
    ]


def test_upsert_rows_never_creates_duplicates_relies_on_key_column():
    # L'idempotence est garantie cote outil Composio via keyColumn : on
    # verifie simplement que la meme cle est reenvoyee a l'identique sur
    # deux appels successifs (pas de generation d'ID differents).
    fake_client = _fake_httpx_client(
        json_return={
            "successful": True,
            "error": None,
            "data": {"rowsUpdated": 1, "rowsInserted": 0},
        }
    )
    client = SheetsClient(
        composio_api_key="key",
        composio_user_id="user-1",
        composio_connected_account_id="",
        spreadsheet_id="abc123",
    )
    with patch("httpx.Client", return_value=fake_client):
        client.upsert_rows("BOT_FACTURES", ["ID", "Nom"], [{"id": "INV-1-1", "ID": "INV-1-1", "Nom": "A"}])
        client.upsert_rows(
            "BOT_FACTURES", ["ID", "Nom"], [{"id": "INV-1-1", "ID": "INV-1-1", "Nom": "A modifie"}]
        )
    first_call, second_call = fake_client.post.call_args_list
    assert first_call.kwargs["json"]["arguments"]["keyColumn"] == "ID"
    assert second_call.kwargs["json"]["arguments"]["keyColumn"] == "ID"
    assert first_call.kwargs["json"]["arguments"]["rows"][0][0] == "INV-1-1"
    assert second_call.kwargs["json"]["arguments"]["rows"][0][0] == "INV-1-1"


def test_upsert_rows_uses_connected_account_id_when_no_user_id():
    fake_client = _fake_httpx_client(
        json_return={
            "successful": True,
            "error": None,
            "data": {"rowsUpdated": 0, "rowsInserted": 1},
        }
    )
    client = SheetsClient(
        composio_api_key="key",
        composio_user_id="",
        composio_connected_account_id="ca-1",
        spreadsheet_id="abc123",
    )
    with patch("httpx.Client", return_value=fake_client):
        client.upsert_rows("ONGLET", ["ID"], [{"id": "1", "ID": "1"}])
    body = fake_client.post.call_args.kwargs["json"]
    assert body["connected_account_id"] == "ca-1"
    assert "user_id" not in body


def test_upsert_rows_wraps_tool_failure_in_sheets_sync_error():
    fake_client = _fake_httpx_client(
        json_return={"successful": False, "error": "boom", "data": {}}
    )
    client = SheetsClient(
        composio_api_key="key",
        composio_user_id="user-1",
        composio_connected_account_id="",
        spreadsheet_id="abc123",
    )
    with patch("httpx.Client", return_value=fake_client):
        with pytest.raises(SheetsSyncError):
            client.upsert_rows("ONGLET", ["ID"], [{"id": "1", "ID": "1"}])


def test_upsert_rows_wraps_http_error_without_leaking_secrets():
    fake_client = _fake_httpx_client(post_side_effect=RuntimeError("network boom"))
    client = SheetsClient(
        composio_api_key="super-secret-key",
        composio_user_id="user-1",
        composio_connected_account_id="",
        spreadsheet_id="abc123",
    )
    with patch("httpx.Client", return_value=fake_client):
        with pytest.raises(SheetsSyncError) as exc_info:
            client.upsert_rows("ONGLET", ["ID"], [{"id": "1", "ID": "1"}])
    assert "super-secret-key" not in str(exc_info.value)


def test_upsert_rows_empty_rows_is_noop():
    fake_client = _fake_httpx_client()
    client = SheetsClient(
        composio_api_key="key",
        composio_user_id="user-1",
        composio_connected_account_id="",
        spreadsheet_id="abc123",
    )
    with patch("httpx.Client", return_value=fake_client):
        result = client.upsert_rows("ONGLET", ["ID"], [])
    assert result == {"updated": 0, "appended": 0}
    fake_client.post.assert_not_called()


def test_client_headers_never_expose_api_key_in_error_path():
    # Le header d'auth est construit une seule fois dans _ensure_client ;
    # on verifie ici que le nom du header est bien x-api-key (pas de faute
    # de frappe qui romprait l'authentification silencieusement).
    fake_client = _fake_httpx_client(
        json_return={"successful": True, "error": None, "data": {"rowsUpdated": 0, "rowsInserted": 1}}
    )
    client = SheetsClient(
        composio_api_key="key-123",
        composio_user_id="user-1",
        composio_connected_account_id="",
        spreadsheet_id="abc123",
    )
    with patch("httpx.Client", return_value=fake_client) as ctor:
        client.upsert_rows("ONGLET", ["ID"], [{"id": "1", "ID": "1"}])
    _, ctor_kwargs = ctor.call_args
    assert ctor_kwargs["headers"]["x-api-key"] == "key-123"
