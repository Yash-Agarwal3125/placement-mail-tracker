"""Tests for rotating log configuration."""

from logging.handlers import RotatingFileHandler

from placement_mail_tracker.utils.logging_config import setup_logging


def test_setup_logging_uses_rotating_file_handler(tmp_path) -> None:
    log_file = tmp_path / "app.log"

    setup_logging(
        "INFO",
        log_file=log_file,
        max_bytes=1234,
        backup_count=5,
    )

    handlers = [
        handler
        for handler in __import__("logging").getLogger().handlers
        if isinstance(handler, RotatingFileHandler)
    ]

    assert handlers
    assert handlers[0].baseFilename == str(log_file)
    assert handlers[0].maxBytes == 1234
    assert handlers[0].backupCount == 5
