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
    gmail_max_results: int = Field(default=10, alias="GMAIL_MAX_RESULTS")

    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-1.5-flash", alias="GEMINI_MODEL")
    gemini_max_retries: int = Field(default=3, alias="GEMINI_MAX_RETRIES")
    gemini_retry_delay_seconds: float = Field(default=1.0, alias="GEMINI_RETRY_DELAY_SECONDS")

    google_sheet_id: str = Field(default="", alias="GOOGLE_SHEET_ID")
    google_sheet_range: str = Field(default="Sheet1!A1", alias="GOOGLE_SHEET_RANGE")
    google_sheet_name: str = Field(default="Opportunities", alias="GOOGLE_SHEET_NAME")
    google_sheets_credentials_file: str = Field(
        default="config/credentials.json",
        alias="GOOGLE_SHEETS_CREDENTIALS_FILE",
    )
    google_sheets_token_file: str = Field(
        default="config/sheets_token.json",
        alias="GOOGLE_SHEETS_TOKEN_FILE",
    )

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    smtp_email: str = Field(default="", alias="SMTP_EMAIL")
    smtp_app_password: str = Field(default="", alias="SMTP_APP_PASSWORD")
    email_receiver: str = Field(default="", alias="EMAIL_RECEIVER")

    sync_interval_hours: int = Field(default=3, alias="SYNC_INTERVAL_HOURS")

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


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
