"""Central logging setup."""

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive information."""
    
    PATTERNS = [
        (
            re.compile(
                r"(?i)(password|secret|token|api[_-]?key|credentials)"
                r"[\s]*[:=][\s]*['\"]?([^'\"\s,\}]+)['\"]?"
            ),
            r"\1=***REDACTED***",
        ),
        (re.compile(r"(AIza[0-9A-Za-z-_]{35})"), r"***REDACTED_API_KEY***"),
        (re.compile(r"(1//[0-9A-Za-z-_]+)"), r"***REDACTED_OAUTH_TOKEN***"),
    ]

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        for pattern, repl in self.PATTERNS:
            msg = pattern.sub(repl, msg)
        return msg

def setup_logging(
    level: str = "INFO",
    *,
    log_file: str | Path = "logs/app.log",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure console and file logging for the application."""
    log_path = Path(log_file)
    log_dir = log_path.parent
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = RedactingFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[console_handler, file_handler],
        force=True,
    )
