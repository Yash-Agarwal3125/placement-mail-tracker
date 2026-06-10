from pathlib import Path

path = Path("src/placement_mail_tracker/db/manager.py")
content = path.read_text(encoding="utf-8")

# 1. Update processed_emails table creation
create_pe_old = """            CREATE TABLE IF NOT EXISTS processed_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_message_id TEXT UNIQUE NOT NULL,
                opportunity_id INTEGER,
                subject TEXT NOT NULL,
                sender TEXT,
                received_at TEXT,
                filter_score INTEGER,
                filter_decision TEXT,
                processed_status TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id)
            );"""

create_pe_new = """            CREATE TABLE IF NOT EXISTS processed_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_message_id TEXT UNIQUE NOT NULL,
                opportunity_id INTEGER,
                subject TEXT NOT NULL,
                sender TEXT,
                received_at TEXT,
                filter_score INTEGER,
                filter_decision TEXT,
                processed_status TEXT NOT NULL,
                error_message TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_retry_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id)
            );"""

content = content.replace(create_pe_old, create_pe_new)


# 2. Update log_processed_email signature and SQL
log_pe_old_sig = """        error_message: str | None = None,
        email_classification: str | None = None,
    ) -> int:"""

log_pe_new_sig = """        error_message: str | None = None,
        email_classification: str | None = None,
        retry_count: int | None = None,
        last_retry_at: str | None = None,
    ) -> int:"""
content = content.replace(log_pe_old_sig, log_pe_new_sig)

log_pe_sql_old = """            INSERT INTO processed_emails (
                gmail_message_id, opportunity_id, subject, sender,
                received_at, filter_score, filter_decision,
                processed_status, error_message, email_classification,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gmail_message_id) DO UPDATE SET
                opportunity_id = excluded.opportunity_id,
                subject = excluded.subject,
                sender = excluded.sender,
                received_at = excluded.received_at,
                filter_score = excluded.filter_score,
                filter_decision = excluded.filter_decision,
                processed_status = excluded.processed_status,
                error_message = excluded.error_message,
                email_classification = excluded.email_classification,
                updated_at = excluded.updated_at;
            \"\"\",
            (
                gmail_message_id,
                opportunity_id,
                subject,
                sender,
                received_at,
                filter_score,
                _serialize_value(filter_decision),
                processed_status,
                error_message,
                email_classification,
                now,
                now,
            ),"""

log_pe_sql_new = """            INSERT INTO processed_emails (
                gmail_message_id, opportunity_id, subject, sender,
                received_at, filter_score, filter_decision,
                processed_status, error_message, email_classification,
                retry_count, last_retry_at,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 0), ?, ?, ?)
            ON CONFLICT(gmail_message_id) DO UPDATE SET
                opportunity_id = excluded.opportunity_id,
                subject = excluded.subject,
                sender = excluded.sender,
                received_at = excluded.received_at,
                filter_score = excluded.filter_score,
                filter_decision = excluded.filter_decision,
                processed_status = excluded.processed_status,
                error_message = excluded.error_message,
                email_classification = excluded.email_classification,
                retry_count = COALESCE(excluded.retry_count, processed_emails.retry_count),
                last_retry_at = COALESCE(excluded.last_retry_at, processed_emails.last_retry_at),
                updated_at = excluded.updated_at;
            \"\"\",
            (
                gmail_message_id,
                opportunity_id,
                subject,
                sender,
                received_at,
                filter_score,
                _serialize_value(filter_decision),
                processed_status,
                error_message,
                email_classification,
                retry_count,
                last_retry_at,
                now,
                now,
            ),"""

content = content.replace(log_pe_sql_old, log_pe_sql_new)

# 3. Update migration to add retry columns
mig_old = """        if "email_classification" not in pe_columns:
            try:
                self.connection.execute(
                    "ALTER TABLE processed_emails ADD COLUMN email_classification TEXT;"
                )
            except sqlite3.OperationalError:
                pass"""

mig_new = """        if "email_classification" not in pe_columns:
            try:
                self.connection.execute(
                    "ALTER TABLE processed_emails ADD COLUMN email_classification TEXT;"
                )
            except sqlite3.OperationalError:
                pass
        if "retry_count" not in pe_columns:
            try:
                self.connection.execute("ALTER TABLE processed_emails ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;")
            except sqlite3.OperationalError:
                pass
        if "last_retry_at" not in pe_columns:
            try:
                self.connection.execute("ALTER TABLE processed_emails ADD COLUMN last_retry_at TEXT;")
            except sqlite3.OperationalError:
                pass"""

content = content.replace(mig_old, mig_new)


# 4. Issue 1: Hash collision in _insert_opportunity
insert_old = """        values = {
            **opportunity,
            "unique_hash": generate_unique_hash(opportunity),
            "source_email_id": source_email_id,"""

insert_new = """        target_hash = generate_unique_hash(opportunity)
        existing_id = self.connection.execute(
            "SELECT id FROM opportunities WHERE unique_hash = ? LIMIT 1", (target_hash,)
        ).fetchone()

        if existing_id:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning("[DB]\\nHash collision detected\\nexisting_id=%s\\nincoming_id=NEW", existing_id[0])
            return existing_id[0]

        values = {
            **opportunity,
            "unique_hash": target_hash,
            "source_email_id": source_email_id,"""

content = content.replace(insert_old, insert_new)


# 5. Issue 1: Hash collision in _update_opportunity_row
update_old = """        values = {
            **opportunity,
            "id": opportunity_id,
            "unique_hash": generate_unique_hash(opportunity),
            "source_email_id": source_email_id,"""

update_new = """        target_hash = generate_unique_hash(opportunity)
        existing_id = self.connection.execute(
            "SELECT id FROM opportunities WHERE unique_hash = ? LIMIT 1", (target_hash,)
        ).fetchone()

        if existing_id and existing_id[0] != opportunity_id:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning("[DB]\\nHash collision detected\\nexisting_id=%s\\nincoming_id=%s", existing_id[0], opportunity_id)
            target_hash = self.connection.execute(
                "SELECT unique_hash FROM opportunities WHERE id = ?", (opportunity_id,)
            ).fetchone()[0]

        values = {
            **opportunity,
            "id": opportunity_id,
            "unique_hash": target_hash,
            "source_email_id": source_email_id,"""

content = content.replace(update_old, update_new)

path.write_text(content, encoding="utf-8")
print("Patched manager.py successfully.")
