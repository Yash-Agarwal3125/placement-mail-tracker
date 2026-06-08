"""Startup configuration validator to prevent runtime errors due to missing setup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from placement_mail_tracker.config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    status: str  # "PASS", "WARNING", or "ERROR"
    message: str
    is_critical: bool
    component: str | None = None


class ConfigValidator:
    """Pre-flight checks for the Placement Mail Tracker to guarantee safe execution."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.results: list[ValidationResult] = []

    def validate_file(
        self,
        file_path: str | Path,
        description: str,
        is_critical: bool = True,
        *,
        component: str | None = None,
    ) -> None:
        """Check if a file exists."""
        path = Path(file_path)
        if path.exists():
            self.results.append(
                ValidationResult(
                    "PASS",
                    f"{description} found at {path}",
                    is_critical,
                    component,
                )
            )
        else:
            status = "ERROR" if is_critical else "WARNING"
            self.results.append(
                ValidationResult(
                    status,
                    f"{description} missing at {path}",
                    is_critical,
                    component,
                )
            )

    def validate_env_var(
        self,
        value: str,
        description: str,
        is_critical: bool = True,
        *,
        component: str | None = None,
    ) -> None:
        """Check if an environment variable (or settings value) is set."""
        if value and value.strip():
            self.results.append(
                ValidationResult(
                    "PASS",
                    f"{description} is configured",
                    is_critical,
                    component,
                )
            )
        else:
            status = "ERROR" if is_critical else "WARNING"
            self.results.append(
                ValidationResult(
                    status,
                    f"{description} is missing or empty",
                    is_critical,
                    component,
                )
            )

    def validate_directory(
        self,
        dir_path: str | Path,
        description: str,
        is_critical: bool = True,
        *,
        component: str | None = None,
    ) -> None:
        """Check if a directory exists. If it's critical and doesn't exist, try to create it."""
        path = Path(dir_path)
        if path.exists() and path.is_dir():
            self.results.append(
                ValidationResult(
                    "PASS",
                    f"{description} exists at {path}",
                    is_critical,
                    component,
                )
            )
        else:
            if is_critical:
                try:
                    path.mkdir(parents=True, exist_ok=True)
                    self.results.append(
                        ValidationResult(
                            "PASS",
                            f"{description} created at {path}",
                            is_critical,
                            component,
                        )
                    )
                except OSError as e:
                    self.results.append(
                        ValidationResult(
                            "ERROR",
                            f"Failed to create {description} at {path}: {e}",
                            is_critical,
                            component,
                        )
                    )
            else:
                self.results.append(
                    ValidationResult(
                        "WARNING",
                        f"{description} missing at {path}",
                        is_critical,
                        component,
                    )
                )

    def run_all_checks(self) -> None:
        """Execute all standard startup validations."""
        self.results.clear()
        strict = self.settings.is_production

        if self.settings.environment not in self.settings.allowed_environments:
            self.results.append(
                ValidationResult(
                    "ERROR",
                    f"APP_ENV must be one of {sorted(self.settings.allowed_environments)}",
                    True,
                    None,
                )
            )

        self.validate_file(".env", ".env file", is_critical=strict)

        # Gmail and Sheets OAuth can be warnings in development/testing. In production,
        # Task Scheduler cannot safely complete an interactive browser login, so both
        # client credentials and existing token files are strict requirements.
        self.validate_file(
            self.settings.gmail_credentials_file,
            "Gmail OAuth credentials.json",
            is_critical=strict,
            component="gmail",
        )
        self.validate_file(
            self.settings.gmail_token_file,
            "Gmail OAuth token.json",
            is_critical=strict,
            component="gmail",
        )
        self.validate_file(
            self.settings.google_sheets_credentials_file,
            "Google Sheets OAuth credentials.json",
            is_critical=strict,
            component="sheets",
        )
        self.validate_file(
            self.settings.google_sheets_token_file,
            "Google Sheets OAuth token.json",
            is_critical=strict,
            component="sheets",
        )
        
        self.validate_env_var(
            self.settings.gemini_api_key,
            "Gemini API Key (GEMINI_API_KEY)",
            is_critical=strict,
        )
        self.validate_env_var(
            self.settings.google_sheet_id,
            "Google Sheet ID (GOOGLE_SHEET_ID)",
            is_critical=strict,
            component="sheets",
        )
        self.validate_env_var(
            self.settings.notification_email,
            "Failure Notification Email (NOTIFICATION_EMAIL)",
            is_critical=False,
            component="notifications",
        )

        self.validate_directory("data", "Data directory", is_critical=True, component="database")
        self.validate_directory("logs", "Logs directory", is_critical=True)

        self.validate_file(
            self.settings.database_path,
            "SQLite database",
            is_critical=False,
            component="database",
        )

    def is_healthy(self) -> bool:
        """Return True if there are NO 'ERROR' statuses."""
        return not any(r.status == "ERROR" for r in self.results)

    def errors(self) -> list[ValidationResult]:
        """Return validation errors."""
        return [result for result in self.results if result.status == "ERROR"]

    def warnings(self) -> list[ValidationResult]:
        """Return validation warnings."""
        return [result for result in self.results if result.status == "WARNING"]

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
