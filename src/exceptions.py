"""Custom exceptions for Codex Code Telegram Bot."""


class CodexCodeTelegramError(Exception):
    """Base exception for Codex Code Telegram Bot."""


class ConfigurationError(CodexCodeTelegramError):
    """Configuration-related errors."""


class MissingConfigError(ConfigurationError):
    """Required configuration is missing."""


class InvalidConfigError(ConfigurationError):
    """Configuration is invalid."""


class SecurityError(CodexCodeTelegramError):
    """Security-related errors."""


class AuthenticationError(SecurityError):
    """Authentication failed."""


class AuthorizationError(SecurityError):
    """Authorization failed."""


class DirectoryTraversalError(SecurityError):
    """Directory traversal attempt detected."""


class CodexError(CodexCodeTelegramError):
    """Codex Code-related errors."""


class CodexTimeoutError(CodexError):
    """Codex Code operation timed out."""


class CodexProcessError(CodexError):
    """Codex Code process execution failed."""


class CodexParsingError(CodexError):
    """Failed to parse Codex Code output."""


class StorageError(CodexCodeTelegramError):
    """Storage-related errors."""


class DatabaseConnectionError(StorageError):
    """Database connection failed."""


class DataIntegrityError(StorageError):
    """Data integrity check failed."""


class TelegramError(CodexCodeTelegramError):
    """Telegram API-related errors."""


class MessageTooLongError(TelegramError):
    """Message exceeds Telegram's length limit."""


class RateLimitError(TelegramError):
    """Rate limit exceeded."""


class RateLimitExceeded(RateLimitError):
    """Rate limit exceeded (alias for compatibility)."""
