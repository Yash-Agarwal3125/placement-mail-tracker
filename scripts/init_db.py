"""Initialize the local SQLite database."""

from placement_mail_tracker.config.settings import get_settings
from placement_mail_tracker.db.connection import get_connection
from placement_mail_tracker.db.schema import create_tables
from placement_mail_tracker.utils.logging_config import setup_logging


def main() -> None:
    """Create required database tables."""
    settings = get_settings()
    setup_logging(settings.log_level)

    with get_connection(settings.database_path) as connection:
        create_tables(connection)

    print(f"Database initialized at: {settings.database_path}")


if __name__ == "__main__":
    main()
