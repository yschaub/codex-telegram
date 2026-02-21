"""Tests for session/integration compatibility helpers."""

from src.bot.utils.session_keys import (
    clear_session_id,
    get_integration,
    get_session_id,
    set_session_id,
)


def test_get_session_id_returns_codex_key():
    user_data = {"codex_session_id": "codex-123"}

    assert get_session_id(user_data) == "codex-123"


def test_get_session_id_returns_none_when_missing():
    user_data = {}

    assert get_session_id(user_data) is None


def test_set_session_id_writes_key():
    user_data = {}

    set_session_id(user_data, "session-xyz")

    assert user_data["codex_session_id"] == "session-xyz"


def test_clear_session_id_clears_key():
    user_data = {"codex_session_id": "a"}

    clear_session_id(user_data)

    assert user_data["codex_session_id"] is None


def test_get_integration_prefers_codex_key():
    codex = object()

    bot_data = {"codex_integration": codex}

    assert get_integration(bot_data) is codex


def test_get_integration_returns_none_when_missing():
    bot_data = {}

    assert get_integration(bot_data) is None
