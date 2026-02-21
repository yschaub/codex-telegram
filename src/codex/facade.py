"""High-level Codex integration facade.

Provides simple interface for bot handlers.
"""

from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import structlog

from ..config.settings import Settings
from .exceptions import CodexProcessError
from .sdk_integration import CodexResponse, CodexSDKManager, StreamUpdate
from .session import SessionManager
from .tool_authorizer import ToolAuthorizer

logger = structlog.get_logger()


class CodexIntegration:
    """Main integration point for Codex."""

    def __init__(
        self,
        config: Settings,
        sdk_manager: Optional[CodexSDKManager] = None,
        session_manager: Optional[SessionManager] = None,
        tool_authorizer: Optional[ToolAuthorizer] = None,
    ):
        """Initialize Codex integration facade."""
        self.config = config
        self.sdk_manager = sdk_manager or CodexSDKManager(config)
        self.session_manager = session_manager
        self.tool_authorizer = tool_authorizer

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
        force_new: bool = False,
    ) -> CodexResponse:
        """Run Codex command with full integration."""
        logger.info(
            "Running Codex command",
            user_id=user_id,
            working_directory=str(working_directory),
            session_id=session_id,
            prompt_length=len(prompt),
            force_new=force_new,
        )

        # If no session_id provided, try to find an existing session for this
        # user+directory combination (auto-resume).
        # Skip auto-resume when force_new is set (e.g. after /new command).
        if not session_id and not force_new:
            existing_session = await self._find_resumable_session(
                user_id, working_directory
            )
            if existing_session:
                session_id = existing_session.session_id
                logger.info(
                    "Auto-resuming existing session for project",
                    session_id=session_id,
                    project_path=str(working_directory),
                    user_id=user_id,
                )

        # Get or create session
        session = await self.session_manager.get_or_create_session(
            user_id, working_directory, session_id
        )

        can_use_tool = self._build_can_use_tool_callback(
            user_id=user_id,
            working_directory=working_directory,
        )

        # Pass through streaming updates
        async def stream_handler(update: StreamUpdate):
            if on_stream:
                try:
                    await on_stream(update)
                except Exception as e:
                    logger.warning("Stream callback failed", error=str(e))

        # Execute command
        try:
            # Continue session if we have an existing session with a real ID
            is_new = getattr(session, "is_new_session", False)
            should_continue = not is_new and bool(session.session_id)

            # For new sessions, don't pass session_id to Codex
            codex_session_id = session.session_id if should_continue else None

            try:
                response = await self._execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=codex_session_id,
                    continue_session=should_continue,
                    stream_callback=stream_handler,
                    can_use_tool=can_use_tool,
                )
            except Exception as resume_error:
                # If resume failed (e.g., session expired on Codex's side),
                # retry as a fresh session
                if should_continue and self._should_retry_fresh_session(resume_error):
                    logger.warning(
                        "Session resume failed, starting fresh session",
                        failed_session_id=codex_session_id,
                        error=str(resume_error),
                    )
                    # Clean up the stale session
                    await self.session_manager.remove_session(session.session_id)

                    # Create a fresh session and retry
                    session = await self.session_manager.get_or_create_session(
                        user_id, working_directory
                    )
                    response = await self._execute(
                        prompt=prompt,
                        working_directory=working_directory,
                        session_id=None,
                        continue_session=False,
                        stream_callback=stream_handler,
                        can_use_tool=can_use_tool,
                    )
                else:
                    raise

            # Update session (assigns real session_id for new sessions)
            await self.session_manager.update_session(session, response)

            # Ensure response has the session's final ID
            response.session_id = session.session_id

            if not response.session_id:
                logger.warning(
                    "No session_id after execution; session cannot be resumed",
                    user_id=user_id,
                )

            logger.info(
                "Codex command completed",
                session_id=response.session_id,
                cost=response.cost,
                duration_ms=response.duration_ms,
                num_turns=response.num_turns,
                is_error=response.is_error,
            )

            return response

        except Exception as e:
            logger.error(
                "Codex command failed",
                error=str(e),
                user_id=user_id,
                session_id=session.session_id,
            )
            raise

    def _should_retry_fresh_session(self, error: Exception) -> bool:
        """Return True when resume errors should fallback to a fresh session."""
        msg = str(error).lower()

        # Authentication/config problems won't be fixed by starting fresh.
        non_retryable_markers = [
            "not logged in",
            "mcp server error",
            "timed out",
        ]
        if any(marker in msg for marker in non_retryable_markers):
            return False

        retryable_markers = [
            "no conversation found",
            "exited with status 1",
            "unexpected argument",
            "no last agent message",
        ]
        if any(marker in msg for marker in retryable_markers):
            return True

        return isinstance(error, CodexProcessError)

    async def _execute(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable] = None,
        can_use_tool: Optional[
            Callable[[str, Dict[str, Any]], Awaitable[Tuple[bool, Optional[str]]]]
        ] = None,
    ) -> CodexResponse:
        """Execute command via SDK."""
        return await self.sdk_manager.execute_command(
            prompt=prompt,
            working_directory=working_directory,
            session_id=session_id,
            continue_session=continue_session,
            stream_callback=stream_callback,
            can_use_tool=can_use_tool,
        )

    async def _find_resumable_session(
        self,
        user_id: int,
        working_directory: Path,
    ) -> Optional["CodexSession"]:  # noqa: F821
        """Find the most recent resumable session for a user in a directory.

        Returns the session if one exists that is non-expired and has a real
        (non-temporary) session ID from Codex. Returns None otherwise.
        """

        sessions = await self.session_manager._get_user_sessions(user_id)

        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory
            and bool(s.session_id)
            and not s.is_expired(self.config.session_timeout_hours)
        ]

        if not matching_sessions:
            return None

        return max(matching_sessions, key=lambda s: s.last_used)

    async def continue_session(
        self,
        user_id: int,
        working_directory: Path,
        prompt: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> Optional[CodexResponse]:
        """Continue the most recent session."""
        logger.info(
            "Continuing session",
            user_id=user_id,
            working_directory=str(working_directory),
            has_prompt=bool(prompt),
        )

        # Get user's sessions
        sessions = await self.session_manager._get_user_sessions(user_id)

        # Find most recent session in this directory (exclude sessions without IDs)
        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory and bool(s.session_id)
        ]

        if not matching_sessions:
            logger.info("No matching sessions found", user_id=user_id)
            return None

        # Get most recent
        latest_session = max(matching_sessions, key=lambda s: s.last_used)

        # Continue session with default prompt if none provided
        # Codex CLI requires a prompt, so we use a placeholder
        return await self.run_command(
            prompt=prompt or "Please continue where we left off",
            working_directory=working_directory,
            user_id=user_id,
            session_id=latest_session.session_id,
            on_stream=on_stream,
        )

    async def get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session information."""
        return await self.session_manager.get_session_info(session_id)

    async def get_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        """Get all sessions for a user."""
        sessions = await self.session_manager._get_user_sessions(user_id)
        return [
            {
                "session_id": s.session_id,
                "project_path": str(s.project_path),
                "created_at": s.created_at.isoformat(),
                "last_used": s.last_used.isoformat(),
                "total_cost": s.total_cost,
                "message_count": s.message_count,
                "tools_used": s.tools_used,
                "expired": s.is_expired(self.config.session_timeout_hours),
            }
            for s in sessions
        ]

    async def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions."""
        return await self.session_manager.cleanup_expired_sessions()

    async def get_tool_stats(self) -> Dict[str, Any]:
        """Get tool usage statistics."""
        if not self.tool_authorizer:
            return {
                "total_calls": 0,
                "by_tool": {},
                "unique_tools": 0,
                "security_violations": 0,
            }
        return self.tool_authorizer.get_tool_stats()

    async def get_user_summary(self, user_id: int) -> Dict[str, Any]:
        """Get comprehensive user summary."""
        session_summary = await self.session_manager.get_user_session_summary(user_id)
        tool_usage = (
            self.tool_authorizer.get_user_tool_usage(user_id)
            if self.tool_authorizer
            else {
                "user_id": user_id,
                "security_violations": 0,
                "violation_types": [],
            }
        )

        return {
            "user_id": user_id,
            **session_summary,
            **tool_usage,
        }

    async def shutdown(self) -> None:
        """Shutdown integration and cleanup resources."""
        logger.info("Shutting down Codex integration")

        await self.cleanup_expired_sessions()

        logger.info("Codex integration shutdown complete")

    def _get_admin_instructions(self, blocked_tools: List[str]) -> str:
        """Generate admin instructions for enabling blocked tools."""
        instructions = []

        # Check if settings file exists
        settings_file = Path(".env")

        if blocked_tools:
            # Get current allowed tools and create merged list without duplicates
            current_tools = [
                "Read",
                "Write",
                "Edit",
                "Bash",
                "Glob",
                "Grep",
                "LS",
                "Task",
                "TaskOutput",
                "MultiEdit",
                "NotebookRead",
                "NotebookEdit",
                "WebFetch",
                "TodoRead",
                "TodoWrite",
                "WebSearch",
            ]
            merged_tools = list(
                dict.fromkeys(current_tools + blocked_tools)
            )  # Remove duplicates while preserving order
            merged_tools_str = ",".join(merged_tools)
            merged_tools_py = ", ".join(f'"{tool}"' for tool in merged_tools)

            instructions.append("**For Administrators:**")
            instructions.append("")

            if settings_file.exists():
                instructions.append(
                    "To enable these tools, add them to your `.env` file:"
                )
                instructions.append("```")
                instructions.append(f'CODEX_ALLOWED_TOOLS="{merged_tools_str}"')
                instructions.append("```")
            else:
                instructions.append("To enable these tools:")
                instructions.append("1. Create a `.env` file in your project root")
                instructions.append("2. Add the following line:")
                instructions.append("```")
                instructions.append(f'CODEX_ALLOWED_TOOLS="{merged_tools_str}"')
                instructions.append("```")

            instructions.append("")
            instructions.append("Or modify the default in `src/config/settings.py`:")
            instructions.append("```python")
            instructions.append("codex_allowed_tools: Optional[List[str]] = Field(")
            instructions.append(f"    default=[{merged_tools_py}],")
            instructions.append('    description="List of allowed Codex tools",')
            instructions.append(")")
            instructions.append("```")

        return "\n".join(instructions)

    def _create_tool_error_message(
        self,
        blocked_tools: List[str],
        allowed_tools: List[str],
        admin_instructions: str,
    ) -> str:
        """Create a comprehensive error message for tool validation failures."""
        tool_list = ", ".join(f"`{tool}`" for tool in blocked_tools)
        allowed_list = (
            ", ".join(f"`{tool}`" for tool in allowed_tools)
            if allowed_tools
            else "None"
        )

        message = [
            "🚫 **Tool Access Blocked**",
            "",
            "Codex tried to use tools that are not currently allowed:",
            f"{tool_list}",
            "",
            "**Why this happened:**",
            "• Codex needs these tools to complete your request",
            "• These tools are not in the allowed tools list",
            "• This is a security feature to control what Codex can do",
            "",
            "**What you can do:**",
            "• Contact the administrator to request access to these tools",
            "• Try rephrasing your request to use different approaches",
            "• Use simpler requests that don't require these tools",
            "",
            "**Currently allowed tools:**",
            f"{allowed_list}",
            "",
            admin_instructions,
        ]

        return "\n".join(message)

    def _build_can_use_tool_callback(
        self,
        user_id: int,
        working_directory: Path,
    ) -> Optional[
        Callable[[str, Dict[str, Any]], Awaitable[Tuple[bool, Optional[str]]]]
    ]:
        """Build per-request tool authorization callback for SDK execution."""
        if not self.tool_authorizer:
            return None

        async def _can_use_tool(
            tool_name: str,
            tool_input: Dict[str, Any],
        ) -> Tuple[bool, Optional[str]]:
            valid, error = await self.tool_authorizer.validate_tool_call(
                tool_name=tool_name,
                tool_input=tool_input,
                working_directory=working_directory,
                user_id=user_id,
            )
            if valid:
                return True, None

            blocked_tools = [tool_name]
            admin_instructions = self._get_admin_instructions(blocked_tools)
            error_message = self._create_tool_error_message(
                blocked_tools=blocked_tools,
                allowed_tools=self.config.codex_allowed_tools or [],
                admin_instructions=admin_instructions,
            )
            return False, error_message

        return _can_use_tool
