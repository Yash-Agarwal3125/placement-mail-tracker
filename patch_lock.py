from pathlib import Path

path = Path("src/placement_mail_tracker/utils/lock_manager.py")
content = path.read_text(encoding="utf-8")

old_log = """                logger.info("[LOCK] Removing stale lock from dead process (PID: %s)", pid)
                self._remove_lock_file()"""

new_log = """                logger.info("[LOCK]\\nRemoving stale lock")
                self._remove_lock_file()"""

old_data = """        lock_data = {
            "pid": os.getpid(),
            "start_time": datetime.now().isoformat(),
            "hostname": socket.gethostname(),
            "script": sys.argv[0] if sys.argv else "unknown",
        }"""

new_data = """        lock_data = {
            "pid": os.getpid(),
            "start_time": datetime.now().isoformat(),
            "hostname": socket.gethostname(),
            "script": sys.argv[0] if sys.argv else "unknown",
            "owner": os.environ.get("USERNAME", "Unknown"),
        }"""

content = content.replace(old_log, new_log)
content = content.replace(old_data, new_data)

path.write_text(content, encoding="utf-8")
print("Patched lock_manager.py")
