import sys
from pathlib import Path

file_path = Path("src/placement_mail_tracker/gmail/filters.py")
content = file_path.read_text(encoding="utf-8")

old_list = """NEGATIVE_KEYWORDS = (
    "club",
    "committee",
    "student organization",
    "event registration",
    "workshop",
    "nptel",
    "academic notice",
    "gravitas",
    "riviera",
    "chapter",
)"""

new_list = """NEGATIVE_KEYWORDS = (
    "club",
    "committee",
    "student organization",
    "event registration",
    "workshop",
    "nptel",
    "academic notice",
    "gravitas",
    "riviera",
    "chapter",
    "fat schedule",
    "cat schedule",
    "exam schedule",
    "patents granted",
    "guest lecture",
    "blood donation",
    "hostel",
    "journal publication",
    "research paper",
    "merchandise",
)"""

if old_list not in content:
    print("old_list not found!")
    sys.exit(1)

content = content.replace(old_list, new_list)
file_path.write_text(content, encoding="utf-8")
print("Patch applied successfully.")
