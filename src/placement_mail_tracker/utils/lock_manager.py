"""Production-grade single instance lock manager.

Ensures that only one instance of the Placement Mail Tracker can run at any given time.
It uses PID liveness checking to safely overwrite stale locks left behind by crashes.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import socket
import sys
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

logger = logging.getLogger(__name__)


def is_process_alive(pid: int) -> bool:
    """Check whether a process is alive without disturbing it."""
    if pid <= 0:
        return False

    if os.name == "nt":
        return _is_process_alive_windows(pid)

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def _is_process_alive_windows(pid: int) -> bool:
    """Check process liveness using Win32 OpenProcess."""
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


class SingleInstanceLock:
    """Context manager for enforcing a single instance of the application."""

    def __init__(self, lock_file: str | Path = "data/tracker.lock") -> None:
        self.lock_file = Path(lock_file)
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._acquired = False

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def acquire(self) -> None:
        """Acquire the lock, exiting the program if another instance is actively running."""
        if self.lock_file.exists():
            try:
                content = self.lock_file.read_text(encoding="utf-8")
                lock_data: dict[str, Any] = json.loads(content)
                pid = lock_data.get("pid")
            except (json.JSONDecodeError, OSError):
                # Corrupted lock file, assume stale and overwrite
                pid = None

            if pid is not None and is_process_alive(pid):
                logger.warning(
                    "[LOCK] Existing active process detected (PID: %s). Exiting gracefully.",
                    pid,
                )
                sys.exit(0)
            else:
                logger.info("[LOCK]\nRemoving stale lock")
                self._remove_lock_file()

        # Write new lock file
        self._write_lock_file()
        self._acquired = True
        logger.info("[LOCK] Lock acquired")

    def release(self) -> None:
        """Release the lock by removing the lock file."""
        if self._acquired:
            self._remove_lock_file()
            self._acquired = False
            logger.info("[LOCK] Lock released")

    def _write_lock_file(self) -> None:
        """Write the lock file with current process metadata."""
        lock_data = {
            "pid": os.getpid(),
            "start_time": datetime.now().isoformat(),
            "hostname": socket.gethostname(),
            "script": sys.argv[0] if sys.argv else "unknown",
            "owner": os.environ.get("USERNAME", "Unknown"),
        }
        try:
            self.lock_file.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")
        except OSError as e:
            logger.error("Failed to write lock file: %s", e)

    def _remove_lock_file(self) -> None:
        """Silently remove the lock file."""
        try:
            if self.lock_file.exists():
                self.lock_file.unlink()
        except OSError as e:
            logger.error("Failed to remove lock file: %s", e)
