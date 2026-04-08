"""Centralized constants for SST."""

# Serialization and diff depth limits
MAX_DEPTH = 100
MAX_DIFF_DEPTH = 1000

# Security: Maximum allowed path length to prevent path traversal attacks
MAX_PATH_LENGTH = 4096

# Security: Allowed script extensions for CLI commands
ALLOWED_SCRIPT_EXTENSIONS = {".py"}

# LLM retry configuration
LLM_MAX_RETRIES = 3
LLM_RETRY_BASE_DELAY = 1.0  # seconds
LLM_RETRY_MAX_DELAY = 30.0  # seconds
LLM_RETRY_EXPONENTIAL_BASE = 2

# Performance: Maximum payload size for serialization (10MB)
MAX_PAYLOAD_SIZE_BYTES = 10 * 1024 * 1024

# Replay timeout default (seconds)
DEFAULT_VERIFY_TIMEOUT = 300
