"""Codex CLI integration behind the existing Codex-compatible interface.

Compatibility goals:
- Preserve CodexResponse / StreamUpdate datatypes
- Preserve CodexSDKManager public methods used elsewhere
- Keep session semantics via ``codex exec resume``
"""

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import structlog

from ..config.settings import Settings
from .exceptions import (
    CodexMCPError,
    CodexProcessError,
    CodexTimeoutError,
    CodexToolValidationError,
)

logger = structlog.get_logger()


def find_codex_cli(
    codex_cli_path: Optional[str] = None,
) -> Optional[str]:
    """Find Codex CLI executable in common locations."""
    import glob

    explicit = [
        codex_cli_path,
        os.environ.get("CODEX_CLI_PATH"),
    ]

    for path in explicit:
        if path and os.path.exists(path) and os.access(path, os.X_OK):
            return path

    in_path = shutil.which("codex")
    if in_path:
        return in_path

    # Best-effort fallback locations
    common_paths = [
        os.path.expanduser("~/.nvm/versions/node/*/bin/codex"),
        os.path.expanduser("~/.npm-global/bin/codex"),
        os.path.expanduser("~/node_modules/.bin/codex"),
        "/usr/local/bin/codex",
        "/usr/bin/codex",
        os.path.expanduser("~/AppData/Roaming/npm/codex.cmd"),
    ]
    for pattern in common_paths:
        matches = glob.glob(pattern)
        for match in matches:
            if os.path.exists(match) and os.access(match, os.X_OK):
                return match

    return None


@dataclass
class CodexResponse:
    """Response object kept for compatibility with existing callers."""

    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class StreamUpdate:
    """Streaming update object kept for compatibility with existing callers."""

    type: str
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None


class CodexSDKManager:
    """Compatibility manager that executes prompts through Codex CLI."""

    def __init__(self, config: Settings):
        self.config = config
        self.codex_path = find_codex_cli(
            codex_cli_path=getattr(config, "codex_cli_path", None),
        )

        if self.codex_path:
            logger.info("Codex CLI detected", codex_path=self.codex_path)
        else:
            logger.warning(
                "Codex CLI not found in PATH or common locations. "
                "Requests will fail until Codex is installed/configured."
            )

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        can_use_tool: Optional[
            Callable[[str, Dict[str, Any]], Awaitable[Tuple[bool, Optional[str]]]]
        ] = None,
    ) -> CodexResponse:
        """Execute command via ``codex exec``."""
        start_time = asyncio.get_running_loop().time()

        output_file = tempfile.NamedTemporaryFile(
            prefix="codex-last-message-", suffix=".txt", delete=False
        )
        output_path = Path(output_file.name)
        output_file.close()

        state: Dict[str, Any] = {
            "session_id": None,
            "turn_count": 0,
            "text_fragments": [],
            "text_fingerprints": set(),
            "tools": [],
            "tool_fingerprints": set(),
            "stderr_lines": [],
            "non_json_stdout": [],
            "event_types": [],
            "event_errors": [],
        }
        process: Optional[asyncio.subprocess.Process] = None

        try:
            cmd = self._build_codex_command(
                prompt=prompt,
                session_id=session_id,
                continue_session=continue_session,
                output_path=output_path,
            )
            env = self._build_environment()

            logger.info(
                "Starting Codex CLI command",
                command=cmd,
                working_directory=str(working_directory),
                continue_session=continue_session,
                session_id=session_id,
            )

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(working_directory),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as e:
                raise CodexProcessError(
                    "Codex CLI not found. Please install Codex CLI and ensure "
                    "`codex` is available in PATH, or set CODEX_CLI_PATH."
                ) from e

            async def _read_stdout() -> None:
                assert process.stdout is not None
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break

                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue

                    if not text.startswith("{"):
                        # Ignore non-JSON line noise (warnings, progress bars, etc.).
                        logger.debug("Codex non-JSON stdout", line=text)
                        state["non_json_stdout"].append(text)
                        continue

                    try:
                        event = json.loads(text)
                    except json.JSONDecodeError:
                        logger.debug("Skipping invalid JSONL line", line=text[:200])
                        continue

                    event_type = str(event.get("type", "unknown"))
                    state["event_types"].append(event_type)
                    logger.debug("Codex JSON event", event_type=event_type)

                    await self._handle_event(
                        event=event,
                        state=state,
                        stream_callback=stream_callback,
                        can_use_tool=can_use_tool,
                    )

            async def _read_stderr() -> None:
                assert process.stderr is not None
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        state["stderr_lines"].append(text)

            await asyncio.wait_for(
                asyncio.gather(_read_stdout(), _read_stderr(), process.wait()),
                timeout=self.config.codex_timeout_seconds,
            )

            duration_ms = int((asyncio.get_running_loop().time() - start_time) * 1000)

            content = ""
            content_from_assistant = False
            if output_path.exists():
                content = output_path.read_text(encoding="utf-8", errors="replace")
                if content.strip():
                    content_from_assistant = True

            if not content.strip():
                content = "\n".join(state["text_fragments"]).strip()
                if content.strip():
                    content_from_assistant = True

            diagnostics = "\n".join(
                [*state["stderr_lines"], *state["non_json_stdout"]]
            ).strip()

            if not content.strip():
                content = (
                    "I could not produce a final response for that request. "
                    "Please try again or rephrase."
                )

            return_code = process.returncode
            if return_code != 0:
                stderr = "\n".join(state["stderr_lines"][-30:]).strip()
                non_json = "\n".join(state["non_json_stdout"][-30:]).strip()
                event_error_text = "\n".join(state["event_errors"][-8:]).strip()
                err_text = (
                    event_error_text
                    or stderr
                    or non_json
                    or f"Codex CLI exited with status {return_code}"
                )
                if not stderr and not non_json and state["event_types"]:
                    err_text = (
                        f"{err_text} (events: {', '.join(state['event_types'][-8:])})"
                    )

                err_lower = err_text.lower()
                if "mcp" in err_lower:
                    raise CodexMCPError(f"MCP server error: {err_text}")

                if "not logged in" in err_lower:
                    raise CodexProcessError(
                        "Codex CLI is not logged in. Run `codex login` on the host "
                        "running this bot, then retry."
                    )

                # Newer Codex versions may emit this warning and non-zero exit when
                # no final assistant artifact is available for --output-last-message.
                # We still salvage streamed text when possible.
                if "no last agent message; wrote empty content" in err_lower:
                    logger.warning(
                        "Codex returned no final assistant artifact; "
                        "falling back to streamed content",
                        return_code=return_code,
                        stderr=err_text,
                    )
                    if not content.strip():
                        content = (
                            "I could not produce a final response for that request. "
                            "Please try again or rephrase."
                        )
                    # Session may not be reliably resumable when command exits non-zero
                    # without producing assistant output.
                    if not content_from_assistant:
                        state["session_id"] = session_id if continue_session else None
                else:
                    # If we recovered meaningful assistant content despite a non-zero
                    # exit, return it instead of failing hard.
                    if content_from_assistant:
                        logger.warning(
                            "Codex exited non-zero but produced assistant content",
                            return_code=return_code,
                            diagnostics=diagnostics[-500:],
                        )
                    else:
                        raise CodexProcessError(f"Codex process error: {err_text}")

            final_session_id = (
                state["session_id"]
                or (session_id if continue_session and session_id else None)
                or ""
            )

            num_turns = state["turn_count"] or (1 if prompt.strip() else 0)

            return CodexResponse(
                content=content,
                session_id=final_session_id,
                cost=0.0,  # Codex CLI does not expose direct USD cost in JSONL stream.
                duration_ms=duration_ms,
                num_turns=num_turns,
                tools_used=state["tools"],
            )

        except asyncio.TimeoutError as e:
            if process and process.returncode is None:
                process.kill()
                try:
                    await process.wait()
                except Exception:
                    pass
            raise CodexTimeoutError(
                f"Codex CLI timed out after {self.config.codex_timeout_seconds}s"
            ) from e
        except CodexToolValidationError:
            if process and process.returncode is None:
                process.kill()
                try:
                    await process.wait()
                except Exception:
                    pass
            raise

        finally:
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to remove temp output file", path=str(output_path))

    def _build_codex_command(
        self,
        prompt: str,
        session_id: Optional[str],
        continue_session: bool,
        output_path: Path,
    ) -> List[str]:
        # Some call sites intentionally pass an empty prompt to mean "continue".
        # Codex expects a non-empty prompt for reliable non-interactive execution.
        if continue_session and not prompt.strip():
            prompt = "Please continue where we left off."

        codex = self.codex_path or "codex"
        cmd: List[str] = [codex, "exec"]

        is_resume = continue_session and bool(session_id)
        if is_resume:
            cmd.append("resume")
            # For `codex exec resume`, options must come before SESSION_ID.
            # Also, current Codex versions don't accept `--sandbox` in resume mode.
            cmd.extend(["--json", "--skip-git-repo-check"])
            if getattr(self.config, "codex_yolo", True):
                cmd.append("--yolo")
        else:
            cmd.extend(["--json", "--skip-git-repo-check"])

            # Default to YOLO mode unless explicitly disabled in config.
            if getattr(self.config, "codex_yolo", True):
                cmd.append("--yolo")
            # Otherwise use explicit sandbox mode for predictable behavior.
            elif self.config.sandbox_enabled:
                cmd.extend(["--sandbox", "workspace-write"])
            else:
                cmd.extend(["--sandbox", "danger-full-access"])

        model = getattr(self.config, "codex_model", None)
        if model:
            cmd.extend(["--model", model])

        max_budget_usd = getattr(self.config, "codex_max_budget_usd", None)
        if max_budget_usd is not None:
            cmd.extend(["-c", f"max_budget_usd={float(max_budget_usd)}"])

        extra_args = getattr(self.config, "codex_extra_args", None) or []
        if is_resume:
            # `codex exec resume` rejects `--sandbox`; strip it even if provided
            # in CODEX_EXTRA_ARGS for normal `codex exec` calls.
            sanitized_args: List[str] = []
            skip_next = False
            for arg in extra_args:
                if skip_next:
                    skip_next = False
                    continue
                if not isinstance(arg, str):
                    continue
                cleaned = arg.strip()
                if not cleaned:
                    continue
                if cleaned == "--sandbox":
                    skip_next = True
                    continue
                if cleaned.startswith("--sandbox="):
                    continue
                sanitized_args.append(cleaned)
            extra_args = sanitized_args

        for arg in extra_args:
            if not isinstance(arg, str):
                continue

            cleaned = arg.strip()
            if not cleaned:
                continue

            yolo_aliases = {"--yolo", "--dangerously-bypass-approvals-and-sandbox"}
            if cleaned in yolo_aliases and any(flag in cmd for flag in yolo_aliases):
                continue

            cmd.append(cleaned)

        if is_resume and session_id:
            cmd.append(session_id)

        cmd.append(prompt)
        return cmd

    def _build_environment(self) -> Dict[str, str]:
        env = os.environ.copy()

        # Empty auth-related vars in .env can shadow valid local Codex login state.
        # Remove blank values before spawning codex.
        for key in (
            "CODEX_HOME",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
            "OPENAI_ORG_ID",
            "OPENAI_PROJECT",
        ):
            val = env.get(key)
            if val is not None and not str(val).strip():
                env.pop(key, None)

        codex_home = getattr(self.config, "codex_home", None)
        if codex_home:
            expanded = Path(codex_home).expanduser()
            if str(expanded).strip() and str(expanded) != ".":
                env["CODEX_HOME"] = str(expanded)
            else:
                env.pop("CODEX_HOME", None)

        codex_cli_path = getattr(self.config, "codex_cli_path", None)
        if codex_cli_path and "CODEX_CLI_PATH" not in env:
            env["CODEX_CLI_PATH"] = codex_cli_path

        return env

    async def _handle_event(
        self,
        event: Dict[str, Any],
        state: Dict[str, Any],
        stream_callback: Optional[Callable[[StreamUpdate], None]],
        can_use_tool: Optional[
            Callable[[str, Dict[str, Any]], Awaitable[Tuple[bool, Optional[str]]]]
        ],
    ) -> None:
        event_type = str(event.get("type", ""))

        thread_id = event.get("thread_id") or event.get("session_id")
        if isinstance(thread_id, str) and thread_id:
            state["session_id"] = thread_id

        if event_type == "turn.started":
            state["turn_count"] += 1

        error_text = self._extract_error_text(event)
        if error_text:
            state["event_errors"].append(error_text)
            if error_text in {
                "error",
                "turn.failed",
                "response.failed",
                "session.failed",
            }:
                logger.warning(
                    "Codex event error",
                    event_type=event_type,
                    error=error_text,
                    event_payload=event,
                )
            else:
                logger.warning(
                    "Codex event error", event_type=event_type, error=error_text
                )

        text_chunks = self._extract_text_chunks(event)
        for text_chunk in text_chunks:
            normalized = text_chunk.strip()
            if not normalized:
                continue
            if normalized in state["text_fingerprints"]:
                continue
            state["text_fingerprints"].add(normalized)
            state["text_fragments"].append(normalized)

            if stream_callback and "delta" in event_type.lower():
                try:
                    await stream_callback(
                        StreamUpdate(
                            type="assistant",
                            content=normalized,
                            metadata={"event_type": event_type},
                        )
                    )
                except Exception as callback_error:
                    logger.warning(
                        "Stream callback failed for text delta",
                        error=str(callback_error),
                    )

        tool_calls = self._extract_tool_calls(event)
        if tool_calls:
            validated_tool_calls: List[Dict[str, Any]] = []
            for tool in tool_calls:
                tool_name = str(tool.get("name", "")).strip()
                tool_input = tool.get("input")
                if not isinstance(tool_input, dict):
                    tool_input = {}

                if can_use_tool and tool_name:
                    try:
                        allowed, reason = await can_use_tool(tool_name, tool_input)
                    except CodexToolValidationError:
                        raise
                    except Exception as callback_error:
                        logger.warning(
                            "Tool validation callback failed",
                            tool_name=tool_name,
                            error=str(callback_error),
                        )
                        raise CodexToolValidationError(
                            f"Tool validation callback failed for {tool_name}: {callback_error}"
                        ) from callback_error

                    if not allowed:
                        raise CodexToolValidationError(
                            reason or f"Tool not allowed: {tool_name}"
                        )

                fingerprint = json.dumps(
                    {
                        "name": tool_name,
                        "input": tool_input,
                    },
                    sort_keys=True,
                )
                if fingerprint in state["tool_fingerprints"]:
                    continue
                state["tool_fingerprints"].add(fingerprint)
                normalized_tool = {
                    "name": tool_name,
                    "input": tool_input,
                }
                state["tools"].append(normalized_tool)
                validated_tool_calls.append(normalized_tool)

            if stream_callback and validated_tool_calls:
                try:
                    await stream_callback(
                        StreamUpdate(
                            type="assistant",
                            tool_calls=validated_tool_calls,
                            metadata={"event_type": event_type},
                        )
                    )
                except Exception as callback_error:
                    logger.warning(
                        "Stream callback failed for tool call",
                        error=str(callback_error),
                    )

    def _extract_text_chunks(self, event: Dict[str, Any]) -> List[str]:
        """Extract assistant-facing text from Codex JSON events."""
        chunks: List[str] = []
        event_type = str(event.get("type", "")).lower()

        # Common delta shape: {"type":"...delta","delta":"..."}
        delta = event.get("delta")
        if isinstance(delta, str) and delta.strip():
            chunks.append(delta.strip())

        # Some events emit text under "text" and "output_text".
        text = event.get("text")
        if isinstance(text, str) and text.strip() and "delta" in event_type:
            chunks.append(text.strip())

        output_text = event.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            chunks.append(output_text.strip())

        # Item/message fallback shape.
        item = event.get("item")
        if isinstance(item, dict):
            chunks.extend(self._extract_text_from_message_like(item))

        message = event.get("message")
        if isinstance(message, dict):
            chunks.extend(self._extract_text_from_message_like(message))

        # Completed response shape from newer Codex JSON output.
        response = event.get("response")
        if isinstance(response, dict):
            response_output = response.get("output")
            if isinstance(response_output, list):
                for output_item in response_output:
                    if isinstance(output_item, dict):
                        chunks.extend(self._extract_text_from_message_like(output_item))

            response_text = response.get("output_text")
            if isinstance(response_text, str) and response_text.strip():
                chunks.append(response_text.strip())

        # Final fallback for non-delta completion events that may carry top-level text.
        if (
            isinstance(text, str)
            and text.strip()
            and (
                "completed" in event_type
                or "assistant" in event_type
                or "response" in event_type
            )
        ):
            chunks.append(text.strip())

        return chunks

    def _extract_error_text(self, event: Dict[str, Any]) -> Optional[str]:
        """Extract structured error text from Codex JSON events."""
        event_type = str(event.get("type", "")).lower()
        if event_type not in {
            "error",
            "turn.failed",
            "response.failed",
            "session.failed",
        }:
            return None

        parts: List[str] = []

        error = event.get("error")
        if isinstance(error, str) and error.strip():
            parts.append(error.strip())
        elif isinstance(error, dict):
            for key in ("message", "detail", "reason", "code", "type"):
                val = error.get(key)
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())

        for key in ("message", "detail", "reason"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())

        errors = event.get("errors")
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    msg = (
                        item.get("message") or item.get("detail") or item.get("reason")
                    )
                    if isinstance(msg, str) and msg.strip():
                        parts.append(msg.strip())

        deduped = list(dict.fromkeys(parts))
        if deduped:
            return " | ".join(deduped)
        return event_type or "unknown codex error"

    def _extract_text_from_message_like(self, message: Dict[str, Any]) -> List[str]:
        chunks: List[str] = []
        role = message.get("role")
        if role is not None and role != "assistant":
            return chunks

        direct_text = message.get("text")
        if isinstance(direct_text, str) and direct_text.strip():
            chunks.append(direct_text.strip())

        content = message.get("content")
        if isinstance(content, str) and content.strip():
            chunks.append(content.strip())
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                part_text = part.get("text")
                if (
                    isinstance(part_text, str)
                    and part_text.strip()
                    and part_type in {"output_text", "text", "message"}
                ):
                    chunks.append(part_text.strip())

                # Some shapes use {"type":"text","content":"..."}
                part_content = part.get("content")
                if (
                    isinstance(part_content, str)
                    and part_content.strip()
                    and part_type in {"output_text", "text", "message"}
                ):
                    chunks.append(part_content.strip())

        return chunks

    def _extract_tool_calls(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        event_type = str(event.get("type", "")).lower()
        tool_calls: List[Dict[str, Any]] = []
        tool_aliases = {
            "read": "Read",
            "read_file": "Read",
            "write": "Write",
            "write_file": "Write",
            "edit": "Edit",
            "edit_file": "Edit",
            "multi_edit": "MultiEdit",
            "multiedit": "MultiEdit",
            "bash": "Bash",
            "shell": "Bash",
            "glob": "Glob",
            "grep": "Grep",
            "ls": "LS",
            "task": "Task",
            "web_fetch": "WebFetch",
            "webfetch": "WebFetch",
            "web_search": "WebSearch",
            "websearch": "WebSearch",
            "todo_read": "TodoRead",
            "todo_write": "TodoWrite",
            "notebook_read": "NotebookRead",
            "notebook_edit": "NotebookEdit",
        }

        # Generic shape: {"tool_name": ..., "input": ...}
        tool_name = event.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            canonical = tool_aliases.get(tool_name.lower())
            if not canonical:
                return []
            tool_calls.append(
                {
                    "name": canonical,
                    "input": (
                        event.get("input")
                        if isinstance(event.get("input"), dict)
                        else {}
                    ),
                }
            )
            return tool_calls

        # Shell command events (best-effort mapping for existing UI)
        command = event.get("command")
        if isinstance(command, str) and command.strip():
            if (
                "exec.command" in event_type
                or "shell" in event_type
                or "bash" in event_type
            ):
                tool_calls.append(
                    {
                        "name": "Bash",
                        "input": {"command": command},
                    }
                )
                return tool_calls

        # Nested generic tool_call object
        nested = event.get("tool_call")
        if isinstance(nested, dict):
            name = nested.get("name")
            if isinstance(name, str) and name:
                canonical = tool_aliases.get(name.lower())
                if not canonical:
                    return []
                tool_calls.append(
                    {
                        "name": canonical,
                        "input": (
                            nested.get("input")
                            if isinstance(nested.get("input"), dict)
                            else {}
                        ),
                    }
                )

        return tool_calls

    def get_active_process_count(self) -> int:
        """Get number of active sessions (always 0, per-request subprocesses)."""
        return 0
