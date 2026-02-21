"""Tests for user-facing backend error formatting."""

from src.bot.handlers.message import _format_process_error


def test_format_process_error_auth_401():
    text = _format_process_error(
        "Codex process error: unexpected status 401 Unauthorized: "
        "Missing bearer or basic authentication in header"
    )
    assert "Codex Authentication Failed" in text
    assert "codex login" in text


def test_format_process_error_resume_flag_mismatch():
    text = _format_process_error(
        "Codex process error: error: unexpected argument '--sandbox' found"
    )
    assert "Codex CLI Flag Mismatch" in text
    assert "/new" in text


def test_format_process_error_exit_status_with_events():
    text = _format_process_error(
        "Codex process error: Codex CLI exited with status 1 "
        "(events: turn.started, error, turn.failed)"
    )
    assert "Codex exited with status 1" in text
    assert "turn.failed" in text

