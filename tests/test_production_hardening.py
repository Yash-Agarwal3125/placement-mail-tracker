import smtplib
from unittest.mock import MagicMock, patch

from placement_mail_tracker.ai.gemini_extractor import GeminiPlacementExtractor
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.notifications.email_notifier import EmailNotifier
from placement_mail_tracker.sheets.sheets_sync import GoogleSheetsSync
from placement_mail_tracker.utils.lock_manager import SingleInstanceLock


# 1. Hash Collision Handling
def test_hash_collision_does_not_crash(db_manager: DatabaseManager, sample_opportunity):
    opp1 = sample_opportunity(company_name="Google", role="SDE")

    # Insert first one
    id1, created1 = db_manager.insert_or_update_opportunity(
        opp1, source_email_id="email1"
    )
    assert created1 is True

    # Insert second one, which will have the same unique_hash. It should fall
    # back to the existing record. _insert_opportunity has explicit hash
    # collision protection that returns the existing id instead of duplicating.
    id2 = db_manager._insert_opportunity(opp1, source_email_id="email2")
    
    assert id1 == id2 # Our fix makes it return the existing ID if it collides!


# 2. Retry limits / permanent failure
def test_email_permanent_failure(db_manager: DatabaseManager):
    msg_id = "test_msg_id_123"
    
    for i in range(1, 6):
        status = "PENDING_EXTRACTION" if i < 5 else "PERMANENT_FAILURE"
        db_manager.log_processed_email(
            gmail_message_id=msg_id,
            subject="Test",
            processed_status=status,
            retry_count=i
        )
        
    row = db_manager.connection.execute(
        "SELECT processed_status, retry_count FROM processed_emails WHERE gmail_message_id=?", 
        (msg_id,)
    ).fetchone()
    
    assert row["processed_status"] == "PERMANENT_FAILURE"
    assert row["retry_count"] == 5


# 3. SMTP Retries
@patch("placement_mail_tracker.notifications.email_notifier.smtplib.SMTP")
@patch("time.sleep")
def test_smtp_resilience_retries(mock_sleep, mock_smtp, mock_settings):
    notifier = EmailNotifier(mock_settings)
    
    # Make SMTP context manager raise SMTPServerDisconnected twice, then succeed
    mock_instance = MagicMock()
    mock_smtp.return_value.__enter__.return_value = mock_instance
    mock_instance.send_message.side_effect = [
        smtplib.SMTPServerDisconnected("Disconnected"),
        TimeoutError("Timeout"),
        None # Success on 3rd attempt
    ]
    
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = "Test"
    
    result = notifier._send_smtp_message_with_retry(msg, "test")
    assert result is True
    assert mock_instance.send_message.call_count == 3
    assert mock_sleep.call_count == 2
    mock_sleep.assert_any_call(2)
    mock_sleep.assert_any_call(5)


# 4. Gemini Retries
@patch("time.sleep")
@patch("placement_mail_tracker.ai.gemini_extractor.GeminiPlacementExtractor._generate_content")
def test_gemini_network_resilience(mock_generate, mock_sleep, mock_settings):
    extractor = GeminiPlacementExtractor(mock_settings)
    
    # ConnectionError twice, then success
    mock_generate.side_effect = [
        ConnectionError("WinError 10053"),
        TimeoutError("Timeout"),
        MagicMock(text='{"company_name": "Google", "role_title": "SDE"}'),
    ]

    # We need to patch _response_text and parse_json_response to not fail
    extractor_mod = "placement_mail_tracker.ai.gemini_extractor"
    with patch(f"{extractor_mod}._response_text", return_value='{}'), \
         patch(
             f"{extractor_mod}.parse_json_response",
             return_value={"company_name": "Google", "role_title": "SDE"},
         ), \
         patch(
             f"{extractor_mod}.validate_extraction_result",
             return_value={"company_name": "Google"},
         ):

        extractor.extract_from_text("Test content")

        assert mock_generate.call_count == 3
        assert mock_sleep.call_count == 2
        # Exact sleep values are not pinned — exponential backoff with jitter;
        # just verify two positive-delay sleeps occurred.
        for call in mock_sleep.call_args_list:
            assert call.args[0] > 0


# 5. Sheets Retries
@patch("time.sleep")
@patch("placement_mail_tracker.sheets.sheets_sync.GoogleSheetsSync._sync_active_opportunities_internal")
def test_sheets_network_resilience(mock_internal, mock_sleep, mock_settings):
    sync = GoogleSheetsSync(mock_settings)
    
    # Throw socket.error twice, then succeed
    mock_internal.side_effect = [
        TimeoutError("Timeout"),
        ConnectionError("ConnError"),
        {"created": 1, "updated": 0, "skipped": 0}
    ]
    
    db_manager = MagicMock()
    result = sync.sync_active_opportunities(db_manager)
    
    assert mock_internal.call_count == 3
    assert mock_sleep.call_count == 2
    mock_sleep.assert_any_call(2)
    mock_sleep.assert_any_call(5)
    assert result == {"created": 1, "updated": 0, "skipped": 0}


# 6. Lock Safety
@patch("placement_mail_tracker.utils.lock_manager.is_process_alive", return_value=False)
def test_stale_lock_cleanup(mock_alive, tmp_path):
    lock_file = tmp_path / "tracker.lock"
    # Create stale lock
    lock_file.write_text('{"pid": 99999}', encoding="utf-8")
    
    lock = SingleInstanceLock(lock_file)
    lock.acquire() # Should clean up stale lock and acquire new one
    
    # Verify new lock has 'owner'
    content = lock_file.read_text(encoding="utf-8")
    assert "owner" in content
    
    lock.release()
