"""Tests du gestionnaire de connexions Google multi-clients (Composio Connect
Links). Aucun appel reseau reel : httpx.Client est entierement mocke."""
from unittest.mock import MagicMock, patch

import pytest

from app.composio_connect import (
    ComposioConnectError,
    ComposioConnectManager,
    composio_user_id_for_chat,
)

AUTH_CONFIGS = {
    "gmail": "ac_gmail",
    "googlesheets": "ac_sheets",
    "googledrive": "ac_drive",
    "googlecalendar": "ac_calendar",
}


def test_composio_user_id_for_chat_is_stable_and_isolated_per_chat():
    assert composio_user_id_for_chat(123) == "telegram_123"
    assert composio_user_id_for_chat(456) != composio_user_id_for_chat(123)


def test_is_configured_false_without_api_key():
    manager = ComposioConnectManager(api_key="", auth_config_by_service=AUTH_CONFIGS)
    assert manager.is_configured is False


def test_is_configured_false_with_missing_auth_config():
    incomplete = dict(AUTH_CONFIGS)
    incomplete["gmail"] = ""
    manager = ComposioConnectManager(api_key="key", auth_config_by_service=incomplete)
    assert manager.is_configured is False


def test_is_configured_true_with_key_and_all_auth_configs():
    manager = ComposioConnectManager(api_key="key", auth_config_by_service=AUTH_CONFIGS)
    assert manager.is_configured is True


def _fake_client_for_links(redirect_urls: dict[str, str]):
    fake_client = MagicMock()

    def _post(path, json):
        auth_config_id = json["auth_config_id"]
        service = next(k for k, v in AUTH_CONFIGS.items() if v == auth_config_id)
        response = MagicMock()
        response.json.return_value = {
            "redirect_url": redirect_urls.get(service),
            "expires_at": "2026-08-20T23:52:00Z",
        }
        return response

    fake_client.post.side_effect = _post
    return fake_client


def test_create_links_raises_when_not_configured():
    manager = ComposioConnectManager(api_key="", auth_config_by_service=AUTH_CONFIGS)
    with pytest.raises(ComposioConnectError):
        manager.create_links(chat_id=42)


def test_create_links_generates_one_link_per_service_scoped_to_chat():
    urls = {k: f"https://connect.composio.dev/link/{k}" for k in AUTH_CONFIGS}
    fake_client = _fake_client_for_links(urls)
    manager = ComposioConnectManager(api_key="key", auth_config_by_service=AUTH_CONFIGS)
    with patch("httpx.Client", return_value=fake_client):
        results = manager.create_links(chat_id=999)

    assert {r.service_key for r in results} == set(AUTH_CONFIGS)
    for call in fake_client.post.call_args_list:
        path, kwargs = call.args, call.kwargs
        assert path[0] == "/api/v3.1/connected_accounts/link"
        assert kwargs["json"]["user_id"] == "telegram_999"
    for r in results:
        assert r.redirect_url == urls[r.service_key]


def test_create_links_skips_service_on_failure_but_returns_others():
    fake_client = MagicMock()

    def _post(path, json):
        if json["auth_config_id"] == AUTH_CONFIGS["gmail"]:
            raise RuntimeError("boom")
        response = MagicMock()
        response.json.return_value = {"redirect_url": "https://connect.composio.dev/link/x"}
        return response

    fake_client.post.side_effect = _post
    manager = ComposioConnectManager(api_key="key", auth_config_by_service=AUTH_CONFIGS)
    with patch("httpx.Client", return_value=fake_client):
        results = manager.create_links(chat_id=1)
    assert "gmail" not in {r.service_key for r in results}
    assert len(results) == 3


def test_get_status_maps_toolkit_slugs_to_service_keys():
    fake_client = MagicMock()
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "items": [
            {"toolkit": {"slug": "gmail"}, "status": "ACTIVE", "id": "ca_1"},
            {"toolkit": {"slug": "googlesheets"}, "status": "INITIATED", "id": "ca_2"},
        ]
    }
    fake_client.get.return_value = fake_response
    manager = ComposioConnectManager(api_key="key", auth_config_by_service=AUTH_CONFIGS)
    with patch("httpx.Client", return_value=fake_client):
        statuses = manager.get_status(chat_id=7)

    assert statuses["gmail"]["status"] == "ACTIVE"
    assert statuses["gmail"]["connected_account_id"] == "ca_1"
    assert statuses["googlesheets"]["status"] == "INITIATED"
    assert statuses["googledrive"]["status"] is None
    assert statuses["googledrive"]["status_label"] == "non connecte"
    params = fake_client.get.call_args.kwargs["params"]
    assert params["user_ids"] == "telegram_7"


def test_get_status_raises_when_api_key_missing():
    manager = ComposioConnectManager(api_key="", auth_config_by_service=AUTH_CONFIGS)
    with pytest.raises(ComposioConnectError):
        manager.get_status(chat_id=1)


def test_get_status_wraps_transport_failure_without_leaking_secret():
    fake_client = MagicMock()
    fake_client.get.side_effect = RuntimeError("network boom")
    manager = ComposioConnectManager(api_key="super-secret", auth_config_by_service=AUTH_CONFIGS)
    with patch("httpx.Client", return_value=fake_client):
        with pytest.raises(ComposioConnectError) as exc_info:
            manager.get_status(chat_id=1)
    assert "super-secret" not in str(exc_info.value)
