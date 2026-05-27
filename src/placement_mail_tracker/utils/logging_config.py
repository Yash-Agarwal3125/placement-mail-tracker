"""Central logging setup."""

import logging
from pathlib import Path


def setup_logging(level: str = "INFO") -> None:
    """Configure console and file logging for the application."""
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "app.log", encoding="utf-8"),
        ],
        force=True,
    )
