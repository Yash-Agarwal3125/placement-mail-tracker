import sys
import os

sys.path.append(os.path.abspath('src'))

from placement_mail_tracker.config.settings import get_settings
from placement_mail_tracker.gmail.gmail_client import GmailClient
import json

def analyze_emails():
    settings = get_settings()
    client = GmailClient(settings)
    
    query = "in:inbox"
    emails = client._search(query=query, max_results=50)
    
    for i, email in enumerate(emails, 1):
        try:
            print(f"--- Email {i} ---")
            print(f"Sender:  {email.sender}".encode('ascii', 'ignore').decode('ascii'))
            print(f"Subject: {email.subject}".encode('ascii', 'ignore').decode('ascii'))
            body_preview = email.body_text.replace('\n', ' ')[:200]
            print(f"Preview: {body_preview}".encode('ascii', 'ignore').decode('ascii'))
            print()
        except Exception as e:
            print(f"Failed to print email {i}: {e}")

if __name__ == "__main__":
    analyze_emails()
