"""Application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    """Typed application settings."""

    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    database_url: str = Field(
        default="sqlite:///data/placement_mail_tracker.db",
        alias="DATABASE_URL",
    )

    gmail_credentials_file: str = Field(
        default="config/credentials.json",
        alias="GMAIL_CREDENTIALS_FILE",
    )
    gmail_token_file: str = Field(default="config/token.json", alias="GMAIL_TOKEN_FILE")
    gmail_query: str = Field(default="newer_than:7d", alias="GMAIL_QUERY")
    gmail_max_results: int = Field(default=100, alias="GMAIL_MAX_RESULTS")

    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    gemini_fallback_models: list[str] = Field(
        default_factory=lambda: [
            "gemini-2.5-flash-lite-preview-06-17",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
        ],
        alias="GEMINI_FALLBACK_MODELS",
    )
    # Quota-aware retry budget: at most gemini_max_models_to_try distinct
    # models (primary + fallbacks, in order), each retried gemini_max_retries
    # time(s). Default 2 models x 1 retry = 2 live calls/email ceiling, down
    # from the previous 6 models x 3 retries = 18 calls/email ceiling, which
    # could burn a full day's free-tier quota (20 requests/day/model) on a
    # single stubborn email.
    gemini_max_retries: int = Field(default=1, alias="GEMINI_MAX_RETRIES")
    gemini_max_models_to_try: int = Field(default=2, alias="GEMINI_MAX_MODELS_TO_TRY")
    gemini_retry_delay_seconds: float = Field(default=2.0, alias="GEMINI_RETRY_DELAY_SECONDS")

    google_sheet_id: str = Field(default="", alias="GOOGLE_SHEET_ID")
    google_sheets_credentials_file: str = Field(
        default="config/credentials.json",
        alias="GOOGLE_SHEETS_CREDENTIALS_FILE",
    )
    google_sheets_token_file: str = Field(
        default="config/sheets_token.json",
        alias="GOOGLE_SHEETS_TOKEN_FILE",
    )

    # Calendar sync (ADR docs/design/03-adr-calendar-sync.md, D4/D6): a third,
    # Calendar-scope-only OAuth stack. Credentials file is intentionally NOT a
    # new setting — it reuses gmail_credentials_file/google_sheets_credentials_file
    # (same client secret, config/credentials.json by default).
    calendar_sync_enabled: bool = Field(default=False, alias="CALENDAR_SYNC_ENABLED")
    calendar_sync_mode: str = Field(default="applied_only", alias="CALENDAR_SYNC_MODE")
    calendar_name: str = Field(default="VIT Placements", alias="CALENDAR_NAME")
    calendar_token_file: str = Field(
        default="config/calendar_token.json",
        alias="CALENDAR_TOKEN_FILE",
    )
    calendar_timezone: str = Field(default="Asia/Kolkata", alias="CALENDAR_TIMEZONE")
    calendar_deadline_reminder_minutes: list[int] = Field(
        default_factory=lambda: [1440],
        alias="CALENDAR_DEADLINE_REMINDER_MINUTES",
    )
    calendar_event_reminder_minutes: list[int] = Field(
        default_factory=lambda: [1440, 60],
        alias="CALENDAR_EVENT_REMINDER_MINUTES",
    )
    calendar_stale_after_hours: float = Field(
        default=48.0, alias="CALENDAR_STALE_AFTER_HOURS"
    )

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    smtp_email: str = Field(default="", alias="SMTP_EMAIL")
    smtp_app_password: str = Field(default="", alias="SMTP_APP_PASSWORD")
    email_receiver: str = Field(default="", alias="EMAIL_RECEIVER")
    notification_email: str = Field(default="", alias="NOTIFICATION_EMAIL")
    digest_send_time: str = Field(default="08:00", alias="DIGEST_SEND_TIME")

    sync_interval_hours: int = Field(default=3, alias="SYNC_INTERVAL_HOURS")
    failure_alert_threshold: int = Field(default=3, alias="FAILURE_ALERT_THRESHOLD")
    heartbeat_inactivity_hours: float = Field(default=6.0, alias="HEARTBEAT_INACTIVITY_HOURS")
    system_health_file: str = Field(
        default="data/system_health.json",
        alias="SYSTEM_HEALTH_FILE",
    )
    heartbeat_file: str = Field(default="data/heartbeat.json", alias="HEARTBEAT_FILE")
    fetch_state_file: str = Field(default="data/fetch_state.json", alias="FETCH_STATE_FILE")
    log_file: str = Field(default="logs/app.log", alias="LOG_FILE")
    log_max_bytes: int = Field(default=10 * 1024 * 1024, alias="LOG_MAX_BYTES")
    log_backup_count: int = Field(default=5, alias="LOG_BACKUP_COUNT")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def database_path(self) -> Path:
        """Return a filesystem path for the configured SQLite database."""
        if not self.database_url.startswith("sqlite:///"):
            msg = "Only sqlite:/// database URLs are supported in the starter project."
            raise ValueError(msg)

        path = Path(self.database_url.replace("sqlite:///", "", 1))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def environment(self) -> str:
        """Return normalized application environment."""
        return self.app_env.strip().lower()

    @property
    def is_development(self) -> bool:
        """Return True when running in development mode."""
        return self.environment == "development"

    @property
    def is_testing(self) -> bool:
        """Return True when running in testing mode."""
        return self.environment == "testing"

    @property
    def is_production(self) -> bool:
        """Return True when running in production mode."""
        return self.environment == "production"

    @property
    def allowed_environments(self) -> set[str]:
        """Return supported environment names."""
        return {"development", "testing", "production"}


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
