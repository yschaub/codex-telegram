"""Tests for the Codex-backed compatibility integration layer."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.codex.exceptions import (
    CodexProcessError,
    CodexTimeoutError,
    CodexToolValidationError,
)
from src.codex.sdk_integration import CodexResponse, CodexSDKManager, StreamUpdate
from src.config.settings import Settings


class _Stream:
    """Async readline stream backed by a list of byte lines."""

    def __init__(self, lines=None, delay: float = 0.0):
        self._lines = list(lines or [])
        self._delay = delay

    async def readline(self) -> bytes:
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _MockProcess:
    """Minimal asyncio subprocess-compatible mock."""

    def __init__(self, stdout_lines=None, stderr_lines=None, returncode=0, delay=0.0):
        self.stdout = _Stream(stdout_lines, delay=delay)
        self.stderr = _Stream(stderr_lines, delay=delay)
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture
def config(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        codex_timeout_seconds=2,
    )


@pytest.fixture
def manager(config: Settings) -> CodexSDKManager:
    with patch(
        "src.codex.sdk_integration.find_codex_cli", return_value="/usr/bin/codex"
    ):
        return CodexSDKManager(config)


class TestCodexSDKManager:
    async def test_execute_command_success(self, manager: CodexSDKManager):
        called_cmd = []

        async def _create_process(*cmd, **kwargs):
            called_cmd.extend(list(cmd))
            stdout_lines = [
                b'{"type":"thread.started","thread_id":"thread-123"}\n',
                b'{"type":"turn.started"}\n',
                b'{"type":"response.output_text.delta","delta":"hello"}\n',
                b'{"type":"exec.command.started","command":"ls -la"}\n',
            ]
            return _MockProcess(stdout_lines=stdout_lines, returncode=0)

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            response = await manager.execute_command(
                prompt="say hello",
                working_directory=Path("/tmp"),
            )

        assert isinstance(response, CodexResponse)
        assert response.session_id == "thread-123"
        assert "--output-last-message" not in called_cmd
        assert "--yolo" in called_cmd
        assert "--sandbox" not in called_cmd
        assert response.content == "hello"
        assert response.num_turns == 1
        assert response.duration_ms >= 0
        assert response.cost == 0.0
        assert any(tool.get("name") == "Bash" for tool in response.tools_used)

    async def test_execute_command_resume_session(self, manager: CodexSDKManager):
        called_cmd = []

        async def _create_process(*cmd, **kwargs):
            cmd_list = list(cmd)
            called_cmd.extend(cmd_list)

            return _MockProcess(
                stdout_lines=[
                    b'{"type":"turn.started"}\n',
                    b'{"type":"response.output_text.delta","delta":"continued"}\n',
                ],
                returncode=0,
            )

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            response = await manager.execute_command(
                prompt="continue",
                working_directory=Path("/tmp"),
                session_id="thread-existing",
                continue_session=True,
            )

        # codex exec resume --json --skip-git-repo-check <session_id>
        assert called_cmd[0:3] == ["/usr/bin/codex", "exec", "resume"]
        assert "--json" in called_cmd
        assert "--skip-git-repo-check" in called_cmd
        assert called_cmd.index("--json") < called_cmd.index("thread-existing")
        assert called_cmd.index("--skip-git-repo-check") < called_cmd.index(
            "thread-existing"
        )
        assert "--yolo" in called_cmd
        assert "--sandbox" not in called_cmd
        assert "--output-last-message" not in called_cmd
        assert response.session_id == "thread-existing"
        assert response.content == "continued"

    async def test_execute_command_extracts_completed_response_text(
        self, manager: CodexSDKManager
    ):
        async def _create_process(*cmd, **kwargs):
            payload = {
                "type": "response.completed",
                "response": {
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "I can see repositories in this directory.",
                                }
                            ],
                        }
                    ]
                },
            }
            stdout_lines = [json.dumps(payload).encode("utf-8") + b"\n"]
            return _MockProcess(stdout_lines=stdout_lines, returncode=0)

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            response = await manager.execute_command(
                prompt="can you see repos?",
                working_directory=Path("/tmp"),
            )

        assert response.content == "I can see repositories in this directory."

    async def test_execute_command_resume_strips_sandbox_extra_args(
        self, manager: CodexSDKManager
    ):
        manager.config.codex_extra_args = ["--sandbox", "workspace-write", "--search"]
        called_cmd = []

        async def _create_process(*cmd, **kwargs):
            cmd_list = list(cmd)
            called_cmd.extend(cmd_list)

            return _MockProcess(
                stdout_lines=[
                    b'{"type":"turn.started"}\n',
                    b'{"type":"response.output_text.delta","delta":"continued"}\n',
                ],
                returncode=0,
            )

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            await manager.execute_command(
                prompt="continue",
                working_directory=Path("/tmp"),
                session_id="thread-existing",
                continue_session=True,
            )

        assert "--sandbox" not in called_cmd
        assert "--search" in called_cmd
        assert "--output-last-message" not in called_cmd

    async def test_execute_command_can_disable_yolo_and_use_workspace_sandbox(
        self, manager: CodexSDKManager
    ):
        manager.config.codex_yolo = False
        manager.config.sandbox_enabled = True
        called_cmd = []

        async def _create_process(*cmd, **kwargs):
            called_cmd.extend(list(cmd))
            return _MockProcess(
                stdout_lines=[b'{"type":"response.output_text.delta","delta":"ok"}\n'],
                returncode=0,
            )

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            await manager.execute_command(
                prompt="hello",
                working_directory=Path("/tmp"),
            )

        assert "--yolo" not in called_cmd
        assert "--sandbox" in called_cmd
        assert "workspace-write" in called_cmd

    async def test_execute_command_deduplicates_yolo_alias_from_extra_args(
        self, manager: CodexSDKManager
    ):
        manager.config.codex_extra_args = [
            "--yolo",
            "--dangerously-bypass-approvals-and-sandbox",
            "--search",
        ]
        called_cmd = []

        async def _create_process(*cmd, **kwargs):
            called_cmd.extend(list(cmd))
            return _MockProcess(
                stdout_lines=[b'{"type":"response.output_text.delta","delta":"ok"}\n'],
                returncode=0,
            )

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            await manager.execute_command(
                prompt="hello",
                working_directory=Path("/tmp"),
            )

        yolo_aliases = {"--yolo", "--dangerously-bypass-approvals-and-sandbox"}
        assert sum(1 for arg in called_cmd if arg in yolo_aliases) == 1
        assert "--search" in called_cmd

    async def test_execute_command_applies_max_budget_config(
        self, manager: CodexSDKManager
    ):
        manager.config.codex_max_budget_usd = 0.25
        called_cmd = []

        async def _create_process(*cmd, **kwargs):
            called_cmd.extend(list(cmd))
            return _MockProcess(
                stdout_lines=[b'{"type":"response.output_text.delta","delta":"ok"}\n'],
                returncode=0,
            )

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            await manager.execute_command(
                prompt="hello",
                working_directory=Path("/tmp"),
            )

        assert "-c" in called_cmd
        assert "max_budget_usd=0.25" in called_cmd

    async def test_execute_command_stream_callback(self, manager: CodexSDKManager):
        updates = []

        async def _stream_callback(update: StreamUpdate):
            updates.append(update)

        async def _create_process(*cmd, **kwargs):
            stdout_lines = [
                b'{"type":"thread.started","thread_id":"thread-abc"}\n',
                b'{"type":"response.output_text.delta","delta":"partial"}\n',
                b'{"type":"exec.command.started","command":"pytest -q"}\n',
            ]
            return _MockProcess(stdout_lines=stdout_lines, returncode=0)

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            await manager.execute_command(
                prompt="run tests",
                working_directory=Path("/tmp"),
                stream_callback=_stream_callback,
            )

        assert any(update.content == "partial" for update in updates)
        assert any(update.tool_calls for update in updates)

    async def test_execute_command_blocks_tool_with_can_use_tool_callback(
        self, manager: CodexSDKManager
    ):
        async def _create_process(*cmd, **kwargs):
            stdout_lines = [
                b'{"type":"thread.started","thread_id":"thread-abc"}\n',
                b'{"type":"turn.started"}\n',
                b'{"type":"exec.command.started","command":"rm -rf /tmp/demo"}\n',
            ]
            return _MockProcess(stdout_lines=stdout_lines, returncode=0)

        callback_calls = []

        async def _deny_bash(tool_name: str, tool_input: dict):
            callback_calls.append((tool_name, tool_input))
            return False, "Tool policy blocked this operation."

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            with pytest.raises(CodexToolValidationError) as exc_info:
                await manager.execute_command(
                    prompt="delete temp dir",
                    working_directory=Path("/tmp"),
                    can_use_tool=_deny_bash,
                )

        assert "tool policy blocked" in str(exc_info.value).lower()
        assert callback_calls
        assert callback_calls[0][0] == "Bash"

    async def test_execute_command_not_logged_in_error(self, manager: CodexSDKManager):
        async def _create_process(*cmd, **kwargs):
            return _MockProcess(
                stdout_lines=[],
                stderr_lines=[b"Not logged in\n"],
                returncode=1,
            )

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            with pytest.raises(CodexProcessError) as exc_info:
                await manager.execute_command(
                    prompt="hello",
                    working_directory=Path("/tmp"),
                )

        assert "not logged in" in str(exc_info.value).lower()

    async def test_execute_command_no_last_message_warning_is_nonfatal(
        self, manager: CodexSDKManager
    ):
        async def _create_process(*cmd, **kwargs):
            return _MockProcess(
                stdout_lines=[
                    b'{"type":"response.output_text.delta","delta":"partial"}\n'
                ],
                stderr_lines=[
                    b"Warning: no last agent message; wrote empty content to /tmp/out.txt\n"
                ],
                returncode=1,
            )

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            response = await manager.execute_command(
                prompt="hello",
                working_directory=Path("/tmp"),
            )

        assert response.content == "partial"

    async def test_nonzero_exit_with_assistant_content_is_nonfatal(
        self, manager: CodexSDKManager
    ):
        async def _create_process(*cmd, **kwargs):
            return _MockProcess(
                stdout_lines=[
                    b'{"type":"response.output_text.delta","delta":"hello"}\n'
                ],
                stderr_lines=[b"internal warning\n"],
                returncode=1,
            )

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            response = await manager.execute_command(
                prompt="hello",
                working_directory=Path("/tmp"),
            )

        assert response.content == "hello"

    async def test_warning_no_last_message_without_output_does_not_set_new_session(
        self, manager: CodexSDKManager
    ):
        async def _create_process(*cmd, **kwargs):
            return _MockProcess(
                stdout_lines=[b'{"type":"thread.started","thread_id":"new-thread"}\n'],
                stderr_lines=[
                    b"Warning: no last agent message; wrote empty content to /tmp/out.txt\n"
                ],
                returncode=1,
            )

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            response = await manager.execute_command(
                prompt="hello",
                working_directory=Path("/tmp"),
            )

        assert response.content.startswith("I could not produce a final response")
        assert response.session_id == ""

    async def test_error_event_message_is_propagated_on_nonzero_exit(
        self, manager: CodexSDKManager
    ):
        async def _create_process(*cmd, **kwargs):
            stdout_lines = [
                b'{"type":"turn.started"}\n',
                b'{"type":"error","error":{"message":"Approval required for tool execution"}}\n',
                b'{"type":"turn.failed"}\n',
            ]
            return _MockProcess(stdout_lines=stdout_lines, returncode=1)

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            with pytest.raises(CodexProcessError) as exc_info:
                await manager.execute_command(
                    prompt="hello",
                    working_directory=Path("/tmp"),
                )

        assert "approval required" in str(exc_info.value).lower()

    async def test_event_error_takes_precedence_over_stderr_on_nonzero_exit(
        self, manager: CodexSDKManager
    ):
        async def _create_process(*cmd, **kwargs):
            stdout_lines = [
                b'{"type":"turn.started"}\n',
                b'{"type":"error","error":{"message":"unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"}}\n',
                b'{"type":"turn.failed"}\n',
            ]
            stderr_lines = [b"WARN codex_core::state_db: record_discrepancy\n"]
            return _MockProcess(
                stdout_lines=stdout_lines,
                stderr_lines=stderr_lines,
                returncode=1,
            )

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            with pytest.raises(CodexProcessError) as exc_info:
                await manager.execute_command(
                    prompt="hello",
                    working_directory=Path("/tmp"),
                )

        error_message = str(exc_info.value).lower()
        assert "401 unauthorized" in error_message
        assert "missing bearer" in error_message

    async def test_execute_command_timeout(self, manager: CodexSDKManager):
        # Make timeout short so test stays fast.
        manager.config.codex_timeout_seconds = 1

        async def _create_process(*cmd, **kwargs):
            return _MockProcess(
                stdout_lines=[b'{"type":"turn.started"}\n'],
                stderr_lines=[],
                returncode=0,
                delay=5.0,
            )

        with patch(
            "src.codex.sdk_integration.asyncio.create_subprocess_exec",
            side_effect=_create_process,
        ):
            with pytest.raises(CodexTimeoutError):
                await manager.execute_command(
                    prompt="slow request",
                    working_directory=Path("/tmp"),
                )

    def test_get_active_process_count(self, manager: CodexSDKManager):
        assert manager.get_active_process_count() == 0

    def test_build_environment_drops_blank_auth_env_vars(
        self, manager: CodexSDKManager, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CODEX_HOME", "")
        monkeypatch.setenv("OPENAI_API_KEY", "")
        monkeypatch.setenv("OPENAI_BASE_URL", "")
        monkeypatch.setenv("OPENAI_API_BASE", "")

        env = manager._build_environment()

        assert "CODEX_HOME" not in env
        assert "OPENAI_API_KEY" not in env
        assert "OPENAI_BASE_URL" not in env
        assert "OPENAI_API_BASE" not in env

    def test_build_environment_sets_codex_home_from_config(
        self, manager: CodexSDKManager, tmp_path: Path
    ):
        manager.config.codex_home = tmp_path / "codex-home"
        env = manager._build_environment()
        assert env["CODEX_HOME"] == str((tmp_path / "codex-home").expanduser())
