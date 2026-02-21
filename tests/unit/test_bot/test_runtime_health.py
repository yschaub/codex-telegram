"""Tests for Codex runtime health helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.bot.utils.runtime_health import get_codex_runtime_health


class _MockProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_runtime_health_reports_missing_cli():
    bot_data = {"codex_integration": SimpleNamespace(sdk_manager=SimpleNamespace(codex_path=None))}
    health = await get_codex_runtime_health(bot_data)
    assert health["cli"] == "missing"
    assert health["auth"] == "unknown"


@pytest.mark.asyncio
async def test_runtime_health_reports_logged_in_and_uses_cache():
    bot_data = {
        "codex_integration": SimpleNamespace(
            sdk_manager=SimpleNamespace(codex_path="/usr/bin/codex")
        )
    }
    process = _MockProcess(
        stdout=b"Logged in using ChatGPT\n",
        stderr=b"",
        returncode=0,
    )

    with patch(
        "src.bot.utils.runtime_health.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ) as mock_exec:
        first = await get_codex_runtime_health(bot_data)
        second = await get_codex_runtime_health(bot_data)

    assert first["cli"] == "available"
    assert first["auth"] == "logged_in"
    assert first == second
    assert mock_exec.await_count == 1


@pytest.mark.asyncio
async def test_runtime_health_reports_not_logged_in():
    bot_data = {
        "codex_integration": SimpleNamespace(
            sdk_manager=SimpleNamespace(codex_path="/usr/bin/codex")
        )
    }
    process = _MockProcess(
        stdout=b"Not logged in\n",
        stderr=b"",
        returncode=1,
    )

    with patch(
        "src.bot.utils.runtime_health.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ):
        health = await get_codex_runtime_health(bot_data)

    assert health["cli"] == "available"
    assert health["auth"] == "not_logged_in"

