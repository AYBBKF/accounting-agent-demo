"""Connexions Google multi-clients via Composio Connect Links.

Chaque client Telegram connecte ses propres comptes Gmail / Google Sheets /
Google Drive / Google Calendar via un lien d'autorisation Composio individuel
(OAuth) : AUCUN mot de passe ni jeton Google ne transite jamais par le bot,
et chaque client n'agit que sur ses propres donnees (isolation garantie
cote Composio par user_id).

Identifiant utilise : "telegram_<chat_id>" (voir `composio_user_id_for_chat`).

Un "auth config" par toolkit est cree une seule fois pour tout le projet
Composio du bot (composio-managed OAuth : pas de client_id/secret Google a
fournir - voir app/config.py). Ce module ne fait que generer des liens et
lire des statuts de connexion pour un `user_id` donne ; il n'effectue aucune
action metier (lecture/ecriture de donnees) - ca reste le role des outils
Composio individuels appeles ailleurs (ex. sheets_client.py, futur trigger
Gmail).

Appelle directement l'API REST Composio via `httpx`, comme sheets_client.py,
pour la meme raison : le SDK officiel `composio` impose `pydantic>=2.10`,
incompatible avec `aiogram==3.15.0` dans cette image.

Aucun secret (cle API Composio) n'est jamais journalise ni renvoye dans un
message d'erreur.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("demo_bot.composio_connect")

COMPOSIO_BASE_URL = "https://backend.composio.dev"
_REQUEST_TIMEOUT_SECONDS = 20.0

# Services geres, dans l'ordre d'affichage du menu /connect.
SERVICES: list[tuple[str, str, str]] = [
    # (cle interne, toolkit slug Composio, libelle affiche)
    ("gmail", "gmail", "Gmail (lecture seule)"),
    ("googlesheets", "googlesheets", "Google Sheets"),
    ("googledrive", "googledrive", "Google Drive"),
    ("googlecalendar", "googlecalendar", "Google Calendar"),
]

_STATUS_LABELS = {
    "ACTIVE": "connecte",
    "INITIATED": "en attente (lien envoye, pas encore valide)",
    "INITIALIZING": "en attente (lien envoye, pas encore valide)",
    "FAILED": "echec, relancer /connect",
    "EXPIRED": "lien expire, relancer /connect",
    "INACTIVE": "inactif, relancer /connect",
    "REVOKED": "revoque, relancer /connect",
}


class ComposioConnectError(RuntimeError):
    pass


def composio_user_id_for_chat(chat_id: int) -> str:
    """Identifiant Composio stable et isole par client Telegram."""
    return f"telegram_{chat_id}"


@dataclass
class ConnectResult:
    service_key: str
    label: str
    redirect_url: str
    expires_at: str | None


class ComposioConnectManager:
    """Enveloppe fine autour de l'API REST Composio pour la gestion des
    connexions multi-clients (Connect Links + statut)."""

    def __init__(self, api_key: str, auth_config_by_service: dict[str, str]) -> None:
        self._api_key = api_key
        self._auth_config_by_service = auth_config_by_service
        self._client = None

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key) and all(self._auth_config_by_service.get(k) for k, _, _ in SERVICES)

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ComposioConnectError("COMPOSIO_API_KEY manquant.")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependance manquante
            raise ComposioConnectError(f"Dependance httpx manquante: {exc}") from exc
        self._client = httpx.Client(
            base_url=COMPOSIO_BASE_URL,
            headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        return self._client

    def create_links(self, chat_id: int) -> list[ConnectResult]:
        """Genere un Connect Link par service pour ce client. Chaque appel
        cree une nouvelle demande de connexion Composio (le client clique le
        lien de son choix dans le message envoye par le bot)."""
        if not self.is_configured:
            raise ComposioConnectError(
                "Connexions Google non configurees sur ce bot "
                "(COMPOSIO_API_KEY ou un COMPOSIO_AUTH_CONFIG_* manquant)."
            )
        client = self._ensure_client()
        user_id = composio_user_id_for_chat(chat_id)
        results: list[ConnectResult] = []
        for service_key, _, label in SERVICES:
            auth_config_id = self._auth_config_by_service[service_key]
            body = {"auth_config_id": auth_config_id, "user_id": user_id}
            try:
                response = client.post("/api/v3.1/connected_accounts/link", json=body)
                response.raise_for_status()
                data = response.json()
            except Exception as exc:  # noqa: BLE001 - jamais de secret dans le log/l'erreur
                logger.warning("Connect Link '%s' impossible pour %s: %s", service_key, user_id, exc)
                continue
            redirect_url = data.get("redirect_url")
            if not redirect_url:
                logger.warning("Connect Link '%s' sans redirect_url pour %s", service_key, user_id)
                continue
            results.append(
                ConnectResult(
                    service_key=service_key,
                    label=label,
                    redirect_url=redirect_url,
                    expires_at=data.get("expires_at"),
                )
            )
        if not results:
            raise ComposioConnectError("Aucun lien de connexion n'a pu etre genere.")
        return results

    def get_status(self, chat_id: int) -> dict[str, dict[str, Any]]:
        """Retourne, pour chaque service, le statut de connexion le plus
        recent de ce client ({"status": "ACTIVE"/... , "label": "..."} ou
        {"status": None, "label": "non connecte"} si aucune connexion)."""
        if not self._api_key:
            raise ComposioConnectError("COMPOSIO_API_KEY manquant.")
        client = self._ensure_client()
        user_id = composio_user_id_for_chat(chat_id)
        try:
            response = client.get(
                "/api/v3.1/connected_accounts",
                params={"user_ids": user_id, "order_by": "updated_at", "order_direction": "desc"},
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ComposioConnectError("Lecture du statut des connexions impossible.") from exc

        by_toolkit: dict[str, dict[str, Any]] = {}
        for item in data.get("items", []):
            toolkit_slug = (item.get("toolkit") or {}).get("slug", "")
            status = item.get("status")
            # Le premier vu par toolkit est le plus recent (order_by=updated_at desc) ;
            # on garde une entree ACTIVE si on en croise une, sinon la plus recente.
            existing = by_toolkit.get(toolkit_slug)
            if existing is None or (status == "ACTIVE" and existing.get("status") != "ACTIVE"):
                by_toolkit[toolkit_slug] = {
                    "status": status,
                    "connected_account_id": item.get("id"),
                }

        result: dict[str, dict[str, Any]] = {}
        for service_key, toolkit_slug, label in SERVICES:
            entry = by_toolkit.get(toolkit_slug)
            status = entry["status"] if entry else None
            result[service_key] = {
                "label": label,
                "status": status,
                "status_label": _STATUS_LABELS.get(status, "non connecte") if status else "non connecte",
                "connected_account_id": entry.get("connected_account_id") if entry else None,
            }
        return result
