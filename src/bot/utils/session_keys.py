"""Helpers for in-memory bot session and integration keys."""

from typing import Any, Mapping, MutableMapping

CODEX_SESSION_KEY = "codex_session_id"
CODEX_INTEGRATION_KEY = "codex_integration"


def get_session_id(user_data: Mapping[str, Any]) -> str | None:
    """Return active Codex session id."""
    return user_data.get(CODEX_SESSION_KEY)


def set_session_id(user_data: MutableMapping[str, Any], session_id: str | None) -> None:
    """Store Codex session id."""
    user_data[CODEX_SESSION_KEY] = session_id


def clear_session_id(user_data: MutableMapping[str, Any]) -> None:
    """Clear Codex session id."""
    set_session_id(user_data, None)


def get_integration(bot_data: Mapping[str, Any]) -> Any:
    """Return Codex integration dependency."""
    return bot_data.get(CODEX_INTEGRATION_KEY)
