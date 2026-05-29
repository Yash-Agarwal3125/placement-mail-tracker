"""Phase 3: Email Filter Tests."""

from placement_mail_tracker.gmail.filters import is_placement_mail

def test_valid_placement_emails():
    valid_subjects = [
        "OA scheduled for Tata Motors",
        "Interview scheduled - Amazon",
        "Additional shortlist released - Waters",
        "Offer released: TCS",
        "PPT announcement for Infosys",
        "Registration open for Deloitte"
    ]
    for subject in valid_subjects:
        decision = is_placement_mail(subject=subject, sender="cdc@vit.ac.in", body="")
        assert decision.is_placement is True

def test_invalid_placement_emails():
    invalid_subjects = [
        "Club recruitment: IEEE",
        "Gravitas committee interview",
        "Workshop notice on AI",
        "Academic circular: Holidays",
        "NPTEL reminder",
        "Event registration",
        "Attendance notice"
    ]
    for subject in invalid_subjects:
        decision = is_placement_mail(subject=subject, sender="noreply@vit.ac.in", body="")
        assert decision.is_placement is False
