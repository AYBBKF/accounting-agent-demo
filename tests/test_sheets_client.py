"""Tests du client Google Sheets. Aucun appel reseau reel : gspread est
entierement mocke (module factice injecte dans sys.modules)."""
import sys
import types
from unittest.mock import MagicMock

import pytest

from app.sheets_client import SheetsClient, SheetsSyncError


def test_is_configured_false_without_credentials():
    client = SheetsClient(service_account_json="", service_account_file="", spreadsheet_id="")
    assert client.is_configured is False


def test_is_configured_true_with_json_and_sheet_id():
    client = SheetsClient(
        service_account_json='{"type": "service_account"}',
        service_account_file="",
        spreadsheet_id="abc123",
    )
    assert client.is_configured is True


def test_sheet_url_none_without_spreadsheet_id():
    client = SheetsClient(service_account_json="x", service_account_file="", spreadsheet_id="")
    assert client.sheet_url() is None


def test_sheet_url_built_from_spreadsheet_id():
    client = SheetsClient(service_account_json="x", service_account_file="", spreadsheet_id="abc123")
    assert client.sheet_url() == "https://docs.google.com/spreadsheets/d/abc123/edit"


def test_upsert_rows_raises_clear_error_when_not_configured():
    client = SheetsClient(service_account_json="", service_account_file="", spreadsheet_id="")
    with pytest.raises(SheetsSyncError):
        client.upsert_rows("ONGLET", ["ID", "Nom"], [{"id": "1", "ID": "1", "Nom": "x"}])


def _install_fake_gspread(monkeypatch, worksheet):
    fake_gspread_module = types.ModuleType("gspread")
    fake_gc = MagicMock()
    fake_sh = MagicMock()
    fake_sh.worksheet.return_value = worksheet
    fake_gc.open_by_key.return_value = fake_sh
    fake_gspread_module.authorize = MagicMock(return_value=fake_gc)
    monkeypatch.setitem(sys.modules, "gspread", fake_gspread_module)

    fake_google_module = types.ModuleType("google")
    fake_oauth2_module = types.ModuleType("google.oauth2")
    fake_service_account_module = types.ModuleType("google.oauth2.service_account")
    fake_service_account_module.Credentials = MagicMock()
    fake_service_account_module.Credentials.from_service_account_info = MagicMock(
        return_value=MagicMock()
    )
    monkeypatch.setitem(sys.modules, "google", fake_google_module)
    monkeypatch.setitem(sys.modules, "google.oauth2", fake_oauth2_module)
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", fake_service_account_module)
    return fake_gc, fake_sh


def test_upsert_rows_appends_new_rows_when_no_existing_ids(monkeypatch):
    ws = MagicMock()
    ws.col_values.return_value = ["ID"]  # seulement l'en-tete, aucune ligne existante
    _install_fake_gspread(monkeypatch, ws)

    client = SheetsClient(
        service_account_json='{"type": "service_account"}',
        service_account_file="",
        spreadsheet_id="abc123",
    )
    result = client.upsert_rows(
        "BOT_FACTURES",
        ["ID", "Nom"],
        [{"id": "INV-1-1", "ID": "INV-1-1", "Nom": "Client DEMO"}],
    )

    assert result == {"updated": 0, "appended": 1}
    ws.append_rows.assert_called_once()
    ws.update.assert_not_called()


def test_upsert_rows_updates_existing_row_in_place_never_duplicates(monkeypatch):
    ws = MagicMock()
    # Ligne 1 = en-tete, ligne 2 = ID existant "INV-1-1"
    ws.col_values.return_value = ["ID", "INV-1-1"]
    _install_fake_gspread(monkeypatch, ws)

    client = SheetsClient(
        service_account_json='{"type": "service_account"}',
        service_account_file="",
        spreadsheet_id="abc123",
    )
    result = client.upsert_rows(
        "BOT_FACTURES",
        ["ID", "Nom"],
        [{"id": "INV-1-1", "ID": "INV-1-1", "Nom": "Client DEMO modifie"}],
    )

    assert result == {"updated": 1, "appended": 0}
    ws.update.assert_called_once()
    ws.append_rows.assert_not_called()


def test_upsert_rows_wraps_open_failure_in_sheets_sync_error(monkeypatch):
    fake_gspread_module = types.ModuleType("gspread")
    fake_gc = MagicMock()
    fake_gc.open_by_key.side_effect = RuntimeError("boom")
    fake_gspread_module.authorize = MagicMock(return_value=fake_gc)
    monkeypatch.setitem(sys.modules, "gspread", fake_gspread_module)

    fake_google_module = types.ModuleType("google")
    fake_oauth2_module = types.ModuleType("google.oauth2")
    fake_service_account_module = types.ModuleType("google.oauth2.service_account")
    fake_service_account_module.Credentials = MagicMock()
    fake_service_account_module.Credentials.from_service_account_info = MagicMock(
        return_value=MagicMock()
    )
    monkeypatch.setitem(sys.modules, "google", fake_google_module)
    monkeypatch.setitem(sys.modules, "google.oauth2", fake_oauth2_module)
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", fake_service_account_module)

    client = SheetsClient(
        service_account_json='{"type": "service_account"}',
        service_account_file="",
        spreadsheet_id="abc123",
    )
    with pytest.raises(SheetsSyncError):
        client.upsert_rows("ONGLET", ["ID"], [{"id": "1", "ID": "1"}])
