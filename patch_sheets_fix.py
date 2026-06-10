from pathlib import Path

path = Path("src/placement_mail_tracker/sheets/sheets_sync.py")
content = path.read_text(encoding="utf-8")

old_try = """        try:
            self.last_error = None"""

new_try = """        if True:
            self.last_error = None"""

content = content.replace(old_try, new_try)

path.write_text(content, encoding="utf-8")
print("Fixed syntax error in sheets_sync.py")
