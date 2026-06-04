"""Startup configuration validator to prevent runtime errors due to missing setup."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from placement_mail_tracker.config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    status: str  # "PASS", "WARNING", or "ERROR"
    message: str
    is_critical: bool


class ConfigValidator:
    """Pre-flight checks for the Placement Mail Tracker to guarantee safe execution."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.results: list[ValidationResult] = []

    def validate_file(self, file_path: str | Path, description: str, is_critical: bool = True) -> None:
        """Check if a file exists."""
        path = Path(file_path)
        if path.exists():
            self.results.append(ValidationResult("PASS", f"{description} found at {path}", is_critical))
        else:
            status = "ERROR" if is_critical else "WARNING"
            self.results.append(ValidationResult(status, f"{description} missing at {path}", is_critical))

    def validate_env_var(self, value: str, description: str, is_critical: bool = True) -> None:
        """Check if an environment variable (or settings value) is set."""
        if value and value.strip():
            self.results.append(ValidationResult("PASS", f"{description} is configured", is_critical))
        else:
            status = "ERROR" if is_critical else "WARNING"
            self.results.append(ValidationResult(status, f"{description} is missing or empty", is_critical))

    def validate_directory(self, dir_path: str | Path, description: str, is_critical: bool = True) -> None:
        """Check if a directory exists. If it's critical and doesn't exist, try to create it."""
        path = Path(dir_path)
        if path.exists() and path.is_dir():
            self.results.append(ValidationResult("PASS", f"{description} exists at {path}", is_critical))
        else:
            if is_critical:
                try:
                    path.mkdir(parents=True, exist_ok=True)
                    self.results.append(ValidationResult("PASS", f"{description} created at {path}", is_critical))
                except OSError as e:
                    self.results.append(ValidationResult("ERROR", f"Failed to create {description} at {path}: {e}", is_critical))
            else:
                self.results.append(ValidationResult("WARNING", f"{description} missing at {path}", is_critical))

    def run_all_checks(self) -> None:
        """Execute all standard startup validations."""
        # 1. Required Files
        self.validate_file(".env", ".env file", is_critical=True)
        self.validate_file(self.settings.google_sheets_credentials_file, "Google/Gmail credentials.json", is_critical=False)
        
        # 2. Required Environment Variables
        self.validate_env_var(self.settings.gemini_api_key, "Gemini API Key (GEMINI_API_KEY)", is_critical=True)
        self.validate_env_var(self.settings.google_sheet_id, "Google Sheet ID (GOOGLE_SHEET_ID)", is_critical=True)
        # Notification email is nice-to-have, but not critical for core sync logic
        self.validate_env_var(
            self.settings.notification_email or self.settings.email_receiver,
            "Notification Email (NOTIFICATION_EMAIL or EMAIL_RECEIVER)",
            is_critical=False
        )

        # 3. Required Directories
        self.validate_directory("data", "Data directory", is_critical=True)
        self.validate_directory("logs", "Logs directory", is_critical=True)

        # 4. Required Database (Warn if missing, but SQLite creates it dynamically so it's not critical)
        self.validate_file(self.settings.database_path, "SQLite database", is_critical=False)

    def is_healthy(self) -> bool:
        """Return True if there are NO 'ERROR' statuses."""
        return not any(r.status == "ERROR" for r in self.results)

    def print_report(self) -> None:
        """Print a formatted Health Report to the logger."""
        passes = [r for r in self.results if r.status == "PASS"]
        warnings = [r for r in self.results if r.status == "WARNING"]
        errors = [r for r in self.results if r.status == "ERROR"]

        logger.info("================================")
        logger.info("CONFIG HEALTH REPORT")
        logger.info("================================")
        logger.info("Environment: %s", self.settings.app_env)
        logger.info("")

        if passes:
            logger.info("PASS:")
            for r in passes:
                logger.info("  [CONFIG] PASS %s", r.message)
            logger.info("")

        if warnings:
            logger.info("WARNINGS:")
            for r in warnings:
                logger.info("  [CONFIG] WARNING %s", r.message)
            logger.info("")

        if errors:
            logger.error("ERRORS:")
            for r in errors:
                logger.error("  [CONFIG] ERROR %s", r.message)
            logger.info("")

        logger.info("================================")
