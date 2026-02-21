"""Tool authorization for Codex executions.

This module provides the runtime policy used by the SDK `can_use_tool` callback.
"""

import shlex
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple

import structlog

from ..config.settings import Settings
from ..security.validators import SecurityValidator

logger = structlog.get_logger()

# Commands that modify the filesystem and should have paths checked
_FS_MODIFYING_COMMANDS: Set[str] = {
    "mkdir",
    "touch",
    "cp",
    "mv",
    "rm",
    "rmdir",
    "ln",
    "install",
    "tee",
}

# Commands that are read-only or don't take filesystem paths
_READ_ONLY_COMMANDS: Set[str] = {
    "cat",
    "ls",
    "head",
    "tail",
    "less",
    "more",
    "which",
    "whoami",
    "pwd",
    "echo",
    "printf",
    "env",
    "printenv",
    "date",
    "wc",
    "sort",
    "uniq",
    "diff",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "realpath",
    "dirname",
    "basename",
}

# Actions / expressions that make ``find`` a filesystem-modifying command
_FIND_MUTATING_ACTIONS: Set[str] = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}


def check_bash_directory_boundary(
    command: str,
    working_directory: Path,
    approved_directory: Path,
) -> Tuple[bool, Optional[str]]:
    """Check if a bash command's absolute paths stay within the approved directory."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Let unparseable commands go to sandbox enforcement.
        return True, None

    if not tokens:
        return True, None

    base_command = Path(tokens[0]).name

    if base_command in _READ_ONLY_COMMANDS:
        return True, None

    if base_command == "find":
        has_mutating_action = any(t in _FIND_MUTATING_ACTIONS for t in tokens[1:])
        if not has_mutating_action:
            return True, None
    elif base_command not in _FS_MODIFYING_COMMANDS:
        return True, None

    resolved_approved = approved_directory.resolve()

    for token in tokens[1:]:
        if token.startswith("-"):
            continue

        if token.startswith("/"):
            resolved = Path(token).resolve()
        else:
            resolved = (working_directory / token).resolve()

        try:
            resolved.relative_to(resolved_approved)
        except ValueError:
            return False, (
                f"Directory boundary violation: '{base_command}' targets "
                f"'{token}' which is outside approved directory "
                f"'{resolved_approved}'"
            )

    return True, None


class ToolAuthorizer(Protocol):
    """Protocol used by CodexIntegration to authorize tool calls."""

    async def validate_tool_call(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        working_directory: Path,
        user_id: int,
    ) -> Tuple[bool, Optional[str]]:
        """Validate a tool call."""

    def get_tool_stats(self) -> Dict[str, Any]:
        """Return aggregate tool stats."""

    def get_user_tool_usage(self, user_id: int) -> Dict[str, Any]:
        """Return per-user tool usage summary."""


class DefaultToolAuthorizer:
    """Default runtime tool authorization policy."""

    def __init__(
        self,
        config: Settings,
        security_validator: Optional[SecurityValidator] = None,
        agentic_mode: bool = False,
    ) -> None:
        self.config = config
        self.security_validator = security_validator
        self.agentic_mode = agentic_mode
        self.tool_usage: Dict[str, int] = defaultdict(int)
        self.security_violations: List[Dict[str, Any]] = []
        self.disable_tool_validation = getattr(config, "disable_tool_validation", False)

    async def validate_tool_call(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        working_directory: Path,
        user_id: int,
    ) -> Tuple[bool, Optional[str]]:
        """Validate tool call before execution."""
        logger.debug(
            "Validating tool call",
            tool_name=tool_name,
            working_directory=str(working_directory),
            user_id=user_id,
        )

        if self.disable_tool_validation:
            logger.debug(
                "Tool name validation disabled; skipping allow/disallow checks",
                tool_name=tool_name,
                user_id=user_id,
            )

        if (
            not self.disable_tool_validation
            and hasattr(self.config, "codex_allowed_tools")
            and self.config.codex_allowed_tools
        ):
            if tool_name not in self.config.codex_allowed_tools:
                violation = {
                    "type": "disallowed_tool",
                    "tool_name": tool_name,
                    "user_id": user_id,
                    "working_directory": str(working_directory),
                }
                self.security_violations.append(violation)
                logger.warning("Tool not allowed", **violation)
                return False, f"Tool not allowed: {tool_name}"

        if (
            not self.disable_tool_validation
            and hasattr(self.config, "codex_disallowed_tools")
            and self.config.codex_disallowed_tools
        ):
            if tool_name in self.config.codex_disallowed_tools:
                violation = {
                    "type": "explicitly_disallowed_tool",
                    "tool_name": tool_name,
                    "user_id": user_id,
                    "working_directory": str(working_directory),
                }
                self.security_violations.append(violation)
                logger.warning("Tool explicitly disallowed", **violation)
                return False, f"Tool explicitly disallowed: {tool_name}"

        if tool_name in ["create_file", "edit_file", "read_file", "Write", "Edit", "Read"]:
            file_path = tool_input.get("path") or tool_input.get("file_path")
            if not file_path:
                return False, "File path required"

            if self.security_validator:
                valid, _, error = self.security_validator.validate_path(
                    file_path, working_directory
                )
                if not valid:
                    violation = {
                        "type": "invalid_file_path",
                        "tool_name": tool_name,
                        "file_path": file_path,
                        "user_id": user_id,
                        "working_directory": str(working_directory),
                        "error": error,
                    }
                    self.security_violations.append(violation)
                    logger.warning("Invalid file path in tool call", **violation)
                    return False, error

        # Skip shell content checks in agentic mode because Codex's sandbox enforces
        # execution boundaries and these checks can reject valid workflows.
        if tool_name in ["bash", "shell", "Bash"] and not self.agentic_mode:
            command = tool_input.get("command", "")
            dangerous_patterns = [
                "rm -rf",
                "sudo",
                "chmod 777",
                "curl",
                "wget",
                "nc ",
                "netcat",
                ">",
                ">>",
                "|",
                "&",
                ";",
                "$(",
                "`",
            ]

            for pattern in dangerous_patterns:
                if pattern in command.lower():
                    violation = {
                        "type": "dangerous_command",
                        "tool_name": tool_name,
                        "command": command,
                        "pattern": pattern,
                        "user_id": user_id,
                        "working_directory": str(working_directory),
                    }
                    self.security_violations.append(violation)
                    logger.warning("Dangerous command detected", **violation)
                    return False, f"Dangerous command pattern detected: {pattern}"

            valid, error = check_bash_directory_boundary(
                command, working_directory, self.config.approved_directory
            )
            if not valid:
                violation = {
                    "type": "directory_boundary_violation",
                    "tool_name": tool_name,
                    "command": command,
                    "user_id": user_id,
                    "working_directory": str(working_directory),
                    "error": error,
                }
                self.security_violations.append(violation)
                logger.warning("Directory boundary violation", **violation)
                return False, error

        self.tool_usage[tool_name] += 1
        logger.debug("Tool call validated successfully", tool_name=tool_name)
        return True, None

    def get_tool_stats(self) -> Dict[str, Any]:
        """Get tool usage statistics."""
        return {
            "total_calls": sum(self.tool_usage.values()),
            "by_tool": dict(self.tool_usage),
            "unique_tools": len(self.tool_usage),
            "security_violations": len(self.security_violations),
        }

    def get_user_tool_usage(self, user_id: int) -> Dict[str, Any]:
        """Get tool usage summary for a user."""
        user_violations = [
            v for v in self.security_violations if v.get("user_id") == user_id
        ]
        return {
            "user_id": user_id,
            "security_violations": len(user_violations),
            "violation_types": list(set(v.get("type") for v in user_violations)),
        }
