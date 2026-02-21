"""Application-wide constants."""

# Version info
APP_NAME = "Codex Telegram Bot"
APP_DESCRIPTION = "Telegram bot for remote Codex CLI access"

# Default limits
DEFAULT_CODEX_TIMEOUT_SECONDS = 300
DEFAULT_CODEX_MAX_TURNS = 10
DEFAULT_CODEX_MAX_COST_PER_USER = 10.0

DEFAULT_RATE_LIMIT_REQUESTS = 10
DEFAULT_RATE_LIMIT_WINDOW = 60
DEFAULT_RATE_LIMIT_BURST = 20

DEFAULT_SESSION_TIMEOUT_HOURS = 24
DEFAULT_MAX_SESSIONS_PER_USER = 5

# Message limits
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
SAFE_MESSAGE_LENGTH = 4000  # Leave room for formatting

# Session limits
MAX_SESSION_LENGTH = 1000  # Maximum messages per session

# File limits
MAX_FILE_SIZE_MB = 10
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Allowed file extensions
ALLOWED_FILE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".java",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".md",
    ".txt",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".xml",
    ".html",
    ".css",
    ".scss",
    ".sql",
    ".sh",
    ".bash",
}

# Security patterns to block
DANGEROUS_PATTERNS = [
    r"\.\.",  # Parent directory
    r"~",  # Home directory
    r"\$",  # Variable expansion
    r"`",  # Command substitution
    r";",  # Command chaining
    r"&&",  # Command chaining
    r"\|\|",  # Command chaining
    r">",  # Redirection
    r"<",  # Redirection
    r"\|",  # Piping
]

# Database defaults
DEFAULT_DATABASE_URL = "sqlite:///data/bot.db"
DEFAULT_BACKUP_RETENTION_DAYS = 30

# Codex CLI defaults (legacy constant name kept for compatibility)
DEFAULT_CODEX_BINARY = "codex"
DEFAULT_CODEX_OUTPUT_FORMAT = "stream-json"

# Logging
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
