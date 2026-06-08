import sys
from pathlib import Path

file_path = Path("src/placement_mail_tracker/gmail/gmail_client.py")
content = file_path.read_text(encoding="utf-8")

old_fetch = """    def fetch_latest_emails(self, max_results: int = 100) -> list[GmailEmail]:
        \"\"\"Fetch latest inbox emails from the configured query.\"\"\"
        query = self.settings.gmail_query
        return self._search(query=query, max_results=max_results)

    def fetch_recent_messages(self, max_results: int = 100) -> list[dict[str, Any]]:
        \"\"\"Fetch latest inbox emails from the current day as dictionaries.\"\"\"
        emails = self.fetch_latest_emails(max_results=max_results)
        return [asdict(email) for email in emails]"""

new_fetch = """    def fetch_emails_since(self, timestamp_seconds: int, max_results: int = 500) -> list[GmailEmail]:
        \"\"\"Fetch inbox emails newer than the specified Unix timestamp.\"\"\"
        query = f"in:inbox after:{timestamp_seconds}"
        return self._search(query=query, max_results=max_results)

    def fetch_recent_messages_since(self, timestamp_seconds: int, max_results: int = 500) -> list[dict[str, Any]]:
        \"\"\"Fetch inbox emails newer than the specified timestamp as dictionaries.\"\"\"
        emails = self.fetch_emails_since(timestamp_seconds=timestamp_seconds, max_results=max_results)
        return [asdict(email) for email in emails]"""

if old_fetch not in content:
    print("old_fetch not found!")
    
    # Try searching for original fetch_latest_emails
    old_fetch_fallback = """    def fetch_latest_emails(self, max_results: int = 100) -> list[GmailEmail]:
        \"\"\"Fetch latest inbox emails from the current day.\"\"\"
        from datetime import datetime

        today_str = datetime.now().strftime("%Y/%m/%d")
        query = f"in:inbox after:{today_str}"
        return self._search(query=query, max_results=max_results)

    def fetch_recent_messages(self, max_results: int = 100) -> list[dict[str, Any]]:
        \"\"\"Fetch latest inbox emails from the current day as dictionaries.\"\"\"
        emails = self.fetch_latest_emails(max_results=max_results)
        return [asdict(email) for email in emails]"""
    
    if old_fetch_fallback in content:
        content = content.replace(old_fetch_fallback, new_fetch)
        print("Fallback old_fetch used and replaced.")
    else:
        print("Fallback also not found. Cannot patch.")
        sys.exit(1)
else:
    content = content.replace(old_fetch, new_fetch)
    print("Patch applied successfully.")

file_path.write_text(content, encoding="utf-8")
