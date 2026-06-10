from pathlib import Path

path = Path("src/placement_mail_tracker/scheduler/runner.py")
content = path.read_text(encoding="utf-8")

# 1. Update pending_records query
pending_old = """        pending_records = self.connection.execute(
            \"\"\"
            SELECT gmail_message_id
            FROM processed_emails
            WHERE processed_status = 'PENDING_EXTRACTION';
            \"\"\"
        ).fetchall()
        pending_ids = [row[0] for row in pending_records]"""

pending_new = """        pending_records = self.connection.execute(
            \"\"\"
            SELECT gmail_message_id, retry_count
            FROM processed_emails
            WHERE processed_status = 'PENDING_EXTRACTION';
            \"\"\"
        ).fetchall()
        pending_ids = [row[0] for row in pending_records]
        pending_counts = {row[0]: row[1] for row in pending_records}"""

content = content.replace(pending_old, pending_new)

# 2. Update already_processed query to include PERMANENT_FAILURE
already_old = """            already_processed = self.connection.execute(
                \"\"\"
                SELECT id
                FROM processed_emails
                WHERE gmail_message_id = ?
                  AND processed_status IN ('processed', 'skipped')
                LIMIT 1;
                \"\"\",
                (msg_id,),
            ).fetchone()"""

already_new = """            already_processed = self.connection.execute(
                \"\"\"
                SELECT id
                FROM processed_emails
                WHERE gmail_message_id = ?
                  AND processed_status IN ('processed', 'skipped', 'PERMANENT_FAILURE')
                LIMIT 1;
                \"\"\",
                (msg_id,),
            ).fetchone()"""

content = content.replace(already_old, already_new)

# 3. Update exception block for PENDING_EXTRACTION
exc_old = """                self.database.log_processed_email(
                    gmail_message_id=msg_id,
                    subject=subject,
                    sender=sender,
                    received_at=timestamp,
                    filter_score=filter_score,
                    filter_decision=decision,
                    processed_status="PENDING_EXTRACTION",
                    error_message=str(e),
                )"""

exc_new = """                retry_count = pending_counts.get(msg_id, 0) + 1
                status = "PENDING_EXTRACTION"
                if retry_count >= 5:
                    status = "PERMANENT_FAILURE"
                    logger.error("[RETRY]\\nEmail moved to permanent failure state")
                    
                self.database.log_processed_email(
                    gmail_message_id=msg_id,
                    subject=subject,
                    sender=sender,
                    received_at=timestamp,
                    filter_score=filter_score,
                    filter_decision=decision,
                    processed_status=status,
                    error_message=str(e),
                    retry_count=retry_count,
                    last_retry_at=datetime.now().isoformat(),
                )"""

content = content.replace(exc_old, exc_new)

# 4. Update heartbeat.json writing
hb_old = """        heartbeat_data = {
            "last_run": datetime.now().isoformat(),
            "status": "success","""

hb_new = """        run_source = "MANUAL" if sys.stdout.isatty() else "TASK_SCHEDULER"
        logger.info("[RUN]\\nSource=%s", run_source)
        heartbeat_data = {
            "last_run": datetime.now().isoformat(),
            "status": "success",
            "run_source": run_source,"""

# Add import sys if it doesn't exist, though it probably does. Wait, runner.py might already have sys.
# I'll just rely on `import sys` being at the top, or I can add it if missing.
if "import sys" not in content:
    content = "import sys\n" + content

content = content.replace(hb_old, hb_new)

path.write_text(content, encoding="utf-8")
print("Patched runner.py successfully.")
