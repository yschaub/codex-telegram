"""Codex-specific exceptions."""


class CodexError(Exception):
    """Base Codex error."""


class CodexTimeoutError(CodexError):
    """Operation timed out."""


class CodexProcessError(CodexError):
    """Process execution failed."""


class CodexParsingError(CodexError):
    """Failed to parse output."""


class CodexSessionError(CodexError):
    """Session management error."""


class CodexMCPError(CodexError):
    """MCP server connection or configuration error."""

    def __init__(self, message: str, server_name: str = None):
        super().__init__(message)
        self.server_name = server_name


class CodexToolValidationError(CodexError):
    """Tool validation failed during Codex execution."""

    def __init__(
        self, message: str, blocked_tools: list = None, allowed_tools: list = None
    ):
        super().__init__(message)
        self.blocked_tools = blocked_tools or []
        self.allowed_tools = allowed_tools or []
