import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from placement_mail_tracker.utils.lock_manager import SingleInstanceLock, is_process_alive


def test_is_process_alive():
    # Current process should always be alive
    assert is_process_alive(os.getpid()) is True

    # A very large PID (or a negative one) should not exist
    # Note: On Windows, some system PIDs might exist, but 999999 is safely unallocated
    assert is_process_alive(9999999) is False


def test_lock_acquire_and_release(tmp_path: Path):
    lock_file = tmp_path / "test.lock"

    # 1. Acquire lock
    with SingleInstanceLock(lock_file=lock_file):
        assert lock_file.exists()
        
        # Verify content
        content = lock_file.read_text(encoding="utf-8")
        data = json.loads(content)
        assert data["pid"] == os.getpid()
        assert "start_time" in data
        assert "hostname" in data
        assert "script" in data

    # 2. Verify release
    assert not lock_file.exists()


@patch("placement_mail_tracker.utils.lock_manager.is_process_alive")
def test_lock_exits_when_alive_process_exists(mock_is_alive, tmp_path: Path):
    mock_is_alive.return_value = True
    lock_file = tmp_path / "test.lock"

    # Create an existing lock
    lock_data = {"pid": 12345}
    lock_file.write_text(json.dumps(lock_data), encoding="utf-8")

    lock = SingleInstanceLock(lock_file=lock_file)
    with pytest.raises(SystemExit) as exc_info:
        lock.acquire()

    assert exc_info.value.code == 0
    # Lock file should NOT be removed by us because it belongs to the active process
    assert lock_file.exists()


@patch("placement_mail_tracker.utils.lock_manager.is_process_alive")
def test_lock_overwrites_stale_lock(mock_is_alive, tmp_path: Path):
    mock_is_alive.return_value = False
    lock_file = tmp_path / "test.lock"

    # Create an existing STALE lock
    lock_data = {"pid": 54321}
    lock_file.write_text(json.dumps(lock_data), encoding="utf-8")

    # It should successfully acquire without exiting
    with SingleInstanceLock(lock_file=lock_file):
        # File should be overwritten with our PID
        content = lock_file.read_text(encoding="utf-8")
        data = json.loads(content)
        assert data["pid"] == os.getpid()

    # Should be released at the end
    assert not lock_file.exists()


def test_lock_overwrites_corrupted_lock(tmp_path: Path):
    lock_file = tmp_path / "test.lock"

    # Create an existing CORRUPT lock
    lock_file.write_text("invalid json { content", encoding="utf-8")

    # It should successfully acquire without exiting
    with SingleInstanceLock(lock_file=lock_file):
        content = lock_file.read_text(encoding="utf-8")
        data = json.loads(content)
        assert data["pid"] == os.getpid()

    assert not lock_file.exists()


def test_lock_cleans_up_on_exception(tmp_path: Path):
    lock_file = tmp_path / "test.lock"

    try:
        with SingleInstanceLock(lock_file=lock_file):
            assert lock_file.exists()
            raise ValueError("Something went wrong!")
    except ValueError:
        pass

    # The context manager should have released the lock
    assert not lock_file.exists()
