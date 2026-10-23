from placement_mail_tracker.calendar_sync.derive import is_applied, is_missed_deadline


def test_progressed_drive_counts_as_applied_even_if_my_status_not_applied():
    opp = {"my_status": "NOT_APPLIED", "current_status": "OA", "deadline": "2020-01-01T10:00"}
    assert is_applied(opp)
    assert not is_missed_deadline(opp)


def test_plain_open_drive_with_passed_deadline_is_still_missed():
    opp = {"my_status": "NOT_APPLIED", "current_status": "OPEN", "deadline": "2020-01-01T10:00"}
    assert not is_applied(opp)
    assert is_missed_deadline(opp)
