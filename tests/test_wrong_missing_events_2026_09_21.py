"""Module tests for the 2026-09-21 wrong/missing-event investigation
(PLAN.md). Every fixture below is real text captured from the user's own
Gmail inbox during that investigation -- not synthetic. Each test documents
which root cause (RC1-RC5) it covers.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from placement_mail_tracker.config.user_profile import UserProfile
from placement_mail_tracker.extraction.confirmation import (
    detect_confirmation_tier,
    extract_company_from_confirmation_subject,
    is_eligibility_only_subject,
)
from placement_mail_tracker.extraction.rule_engine import (
    classify_email,
    extract_from_email,
    is_ppt_mail,
)
from placement_mail_tracker.scheduler.runner import (
    _MULTI_DATE_BY_BATCH_RE,
    PlacementTrackerRunner,
)

_CDC_SENDER = "VIT - Soft Skill Assessments <noreply.cdcinfo@vitstudent.ac.in>"


# ---------------------------------------------------------------------------
# RC1 -- "eligible for X" is an invitation, not a confirmation of application
# ---------------------------------------------------------------------------


class TestRC1EligibilityIsNotApplication:
    FAREPORTAL_ELIGIBLE_SUBJECT = "Congratulations! You're Eligible for Fareportal Placement Drive"
    FAREPORTAL_ELIGIBLE_BODY = (
        "Placement Drive Invitation\nDear Yash Agarwal,\nCongratulations! Based on your "
        "profile, you are eligible to participate in the upcoming placement drive with "
        "Fareportal.\nDrive Details:\nDrive Name:\nFareportal\nDrive Number:\n"
        "pat-PL-2026-1299\nCompany:\nFareportal\nPlease log in to your placement portal "
        "to confirm your participation and view additional details about the drive.\n"
        "Best regards, Dr. V. Samuel Rajkumar,\nDirector - Career Development Center, VIT"
    )
    FAREPORTAL_CONFIRMED_SUBJECT = "Confirmed: Your Registration for Fareportal Placement Drive"
    FAREPORTAL_CONFIRMED_BODY = (
        "Registration Confirmed!\nDear Yash Agarwal,\nGreat news! This email confirms "
        "your successful registration for the upcoming placement drive. We're excited "
        "to have you participate in this opportunity.\nDrive Information:\nDrive Name:\n"
        "Fareportal\nDrive Number:\npat-PL-2026-1299\nCompany:\nFareportal"
    )

    def test_eligible_subject_is_not_recognized_as_confirming(self):
        assert is_eligibility_only_subject(self.FAREPORTAL_ELIGIBLE_SUBJECT) is True

    def test_confirmed_registration_subject_is_not_eligibility_only(self):
        assert is_eligibility_only_subject(self.FAREPORTAL_CONFIRMED_SUBJECT) is False

    def test_eligible_subject_still_resolves_company_for_drive_tracking(self):
        # Still resolves the drive so it gets tracked -- only the APPLIED
        # write is what must be suppressed (see runner.py's proves_application).
        assert (
            extract_company_from_confirmation_subject(self.FAREPORTAL_ELIGIBLE_SUBJECT)
            == "Fareportal"
        )

    def test_eligible_body_does_not_match_any_confirmed_pattern_family(self):
        tier, _family = detect_confirmation_tier(
            self.FAREPORTAL_ELIGIBLE_SUBJECT, self.FAREPORTAL_ELIGIBLE_BODY
        )
        assert tier == "UNKNOWN"


# ---------------------------------------------------------------------------
# RC2 -- a drive's own first announcement isn't a personal round update
# ---------------------------------------------------------------------------


class TestRC2StructuredNewDriveTemplate:
    FAREPORTAL_REGISTRATION_BODY = (
        "*Fareportal Super Dream Internship Registration - 2027 Batch*\n\n"
        "Name of the Company\n*Fareportal*\nCategory\n*Super Dream Internship "
        "Registration - 2027 Batch*\nDate of Visit:\n*Virtual*\n"
        "*Online Assessment : 20-09-2026*\n*Interviews : 22-09-2026*\n"
        "Eligible Branches\nB. Tech ( CSE / IT ) related branches only\n"
        "Eligibility Criteria\n% in X and XII - 75% or 7.5 CGPA\n"
        "Last date for Registration\n*16th September 2026 (09.00 AM)*\nWebsite\n"
        "www.fareportal.com"
    )
    CHARGEBEE_REGISTRATION_BODY = (
        "*Chargebee Super Dream Internship Registration - 2027 Batch*\n\n"
        "Name of the Company\n*Chargebee*\nCategory\n*Super Dream Internship "
        "Registration - 2027 Batch*\nDate of Visit:\n*PPT & Test: 26.09.2026 "
        "12 Noon*\n*Interview Date: 28.09.2026 8:00 am onwards*\n"
        "Eligible Branches\nB. Tech ( CSE / IT ) related branches only\n"
        "Last date for Registration\n*21st September 2026 (10.00 AM)*"
    )
    MALOMATIA_REGISTRATION_BODY = (
        "*DREAM INTERNSHIP 2027 Batch*\n\nName of the Company\nMalomatia India "
        "Technology Services\nCategory\nDream Internship\nDate of Visit:\n"
        "PPT : 01.10.2026 (4 PM) - Virtual Mode\nTest: 13.10.2026 (12.00 pm & "
        "04.00 PM) @ respective campus venues\nInterview Date: 15.10.2026 & "
        "16.10.2026\nEligible Branches\nAll B. Tech (CSE/IT) related branches.\n"
        "Last date for Registration\n5th September 2026"
    )

    def test_fareportal_registration_is_new_drive_not_oa_update(self):
        assert (
            classify_email(
                "Fareportal Super Dream Internship Registration - 2027 Batch",
                self.FAREPORTAL_REGISTRATION_BODY,
            )
            == "NEW_DRIVE"
        )

    def test_chargebee_registration_is_new_drive_not_interview_update(self):
        assert (
            classify_email(
                "Chargebee Super Dream Internship Registration - 2027 Batch",
                self.CHARGEBEE_REGISTRATION_BODY,
            )
            == "NEW_DRIVE"
        )

    def test_malomatia_registration_is_new_drive_not_shortlist_update(self):
        assert (
            classify_email(
                "Malomatia India Technology Services : Registration : Dream "
                "Internship - 2027",
                self.MALOMATIA_REGISTRATION_BODY,
            )
            == "NEW_DRIVE"
        )

    def test_new_drive_classified_mail_never_writes_oa_or_interview_date(
        self, db_manager, mock_settings
    ):
        """Classification alone isn't enough (see runner.py's comment on the
        classification == "NEW_DRIVE" gate): Gemini/rules extract oa_date/
        interview_date from a NEW_DRIVE mail's body regardless of its
        classification, so the runner must also suppress those two specific
        fields for a NEW_DRIVE-classified mail, or the bogus dates land on
        the row anyway and still derive a calendar event."""
        from placement_mail_tracker.scheduler.runner import PlacementTrackerRunner

        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=mock_settings)
        extractor = MagicMock()
        extractor.extract_from_email.return_value = {
            "company_name": "Fareportal",
            "role": "Intern",
            "oa_date": "2026-09-20",
            "interview_date": "2026-09-22",
            "confidence": 0.9,
        }
        msg = {
            "message_id": "new_drive_msg",
            "thread_id": "thread_new_drive",
            "subject": "Fareportal Super Dream Internship Registration - 2027 Batch",
            "sender": "cdc@college.edu",
            "body_text": self.FAREPORTAL_REGISTRATION_BODY,
            "timestamp": "2026-09-15T09:39:37+05:30",
        }
        stats = {
            "processed": 0, "skipped": 0, "errors": 0,
            "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
        }
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        drive = db_manager.fetch_opportunity_by_thread_id("thread_new_drive")
        assert drive is not None
        assert drive["oa_date"] is None
        assert drive["interview_date"] is None

    def test_new_drive_template_ppt_field_also_suppressed_when_not_a_real_ppt_mail(
        self, db_manager, mock_settings
    ):
        """Chargebee's own registration mail has 'Date of Visit: PPT & Test:
        26.09.2026' -- the bare word "PPT" inside a schedule-preview field,
        not a real PPT invitation (is_ppt_mail is False for it). ppt_date
        must be suppressed here exactly like oa_date/interview_date, not
        just when the classification isn't NEW_DRIVE."""
        from placement_mail_tracker.scheduler.runner import PlacementTrackerRunner

        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=mock_settings)
        extractor = MagicMock()
        extractor.extract_from_email.return_value = {
            "company_name": "Chargebee",
            "role": "Intern",
            "oa_date": "2026-09-26",
            "interview_date": "2026-09-28",
            "ppt_date": "2026-09-26",
            "confidence": 0.9,
        }
        msg = {
            "message_id": "new_drive_ppt_msg",
            "thread_id": "thread_new_drive_ppt",
            "subject": "Chargebee Super Dream Internship Registration - 2027 Batch",
            "sender": "cdc@college.edu",
            "body_text": self.CHARGEBEE_REGISTRATION_BODY,
            "timestamp": "2026-09-19T12:48:00+05:30",
        }
        stats = {
            "processed": 0, "skipped": 0, "errors": 0,
            "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
        }
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        drive = db_manager.fetch_opportunity_by_thread_id("thread_new_drive_ppt")
        assert drive is not None
        assert drive["oa_date"] is None
        assert drive["interview_date"] is None
        assert drive["ppt_date"] is None


# ---------------------------------------------------------------------------
# RC3 -- TresVista: thread classification gap + PPT roster mistagged as OA
# ---------------------------------------------------------------------------


class TestRC3TresVista:
    ORIGINAL_ANNOUNCEMENT_SUBJECT = (
        "TresVista Financial Services Super dream offer placement Registration "
        "2027 Batch - Physical interview at vellore campus"
    )
    PPT_MAIL_SUBJECT = (
        "Re: TresVista Financial Services Super dream offer placement "
        "Registration 2027 Batch - Physical interview at vellore campus"
    )
    PPT_MAIL_BODY = (
        "Dear Students,\n\nPlease find below the details to be shared with the "
        "students for the preplacement talk for Analyst DIG profile.\n\nThis is "
        "to invite you to the preplacement talk scheduled for Wednesday, "
        "September 23, 2026, 10:00 AM - 12:00 PM via Microsoft Teams Webinar."
    )

    def test_original_announcement_classifies_new_drive_not_irrelevant(self):
        assert classify_email(self.ORIGINAL_ANNOUNCEMENT_SUBJECT, "") == "NEW_DRIVE"

    def test_ppt_reply_is_recognized_as_a_ppt_mail(self):
        assert is_ppt_mail(self.PPT_MAIL_SUBJECT, self.PPT_MAIL_BODY) is True

    def test_ppt_flavored_oa_update_resolves_roster_event_type_to_ppt(self):
        event_type = PlacementTrackerRunner._resolve_roster_event_type(
            "OA_UPDATE", {}, self.PPT_MAIL_SUBJECT, self.PPT_MAIL_BODY
        )
        assert event_type == "PPT"

    def test_genuine_oa_update_without_ppt_phrasing_still_resolves_to_oa(self):
        event_type = PlacementTrackerRunner._resolve_roster_event_type(
            "OA_UPDATE", {}, "Online assessment scheduled", "Your OA is on 20-09-2026."
        )
        assert event_type == "OA"


# ---------------------------------------------------------------------------
# RC4 -- Caterpillar Hackathon: company-name extraction picked the wrong noun
# ---------------------------------------------------------------------------


class TestRC4CaterpillarHackathonCompanyExtraction:
    HACKATHON_SUBJECT = "CATERPILLAR HACKATHON REGISTRATION - 2027 BATCH"
    HACKATHON_BODY = (
        "Caterpillar is planning to conduct a Hackathon event based hiring for "
        "Final year students, towards the attached JD. Please find below "
        "eligibility criteria for the same.\n\nAll the interested and eligible "
        "students should register in the NEO PAT & Company registration link "
        "on or before 18th September 2026 (10.00 AM)"
    )

    def test_extracts_caterpillar_not_final_year_students(self):
        result = extract_from_email(self.HACKATHON_SUBJECT, self.HACKATHON_BODY)
        assert result.company_name == "Caterpillar"


# ---------------------------------------------------------------------------
# RC5 -- Accenture: multi-date-by-batch announcement flagged, not guessed
# ---------------------------------------------------------------------------


class TestRC5AccentureMultiDateFlag:
    SUBJECT = (
        "Accenture online test is scheduled on 24th, 25th & 26th September "
        "2026 by Respective batches."
    )

    def test_multi_date_by_batch_phrase_is_detected(self):
        assert _MULTI_DATE_BY_BATCH_RE.search(self.SUBJECT) is not None

    def test_single_date_oa_update_does_not_trigger_the_flag(self):
        assert (
            _MULTI_DATE_BY_BATCH_RE.search(
                "Online assessment scheduled on 20-09-2026 by 5.30pm"
            )
            is None
        )


# ---------------------------------------------------------------------------
# Regression fixtures -- real, currently-correct emails must keep classifying
# the same way after all of the above changes.
# ---------------------------------------------------------------------------


class TestRegressionRealCorrectEmails:
    def test_caterpillar_real_shortlist_mail_still_classifies_shortlist_update(self):
        subject = (
            "Hackathon 2026 - Caterpillar Next round of selection process is "
            "scheduled on 24th September 2026 at Anna Auditorium by 8:30 am"
        )
        body = (
            "Hackathon 2026 - Caterpillar Next round of selection process is "
            "scheduled on 24th September 2026 at Anna Auditorium by 8:30 am\n\n"
            "Pls find the below shortlisted list\n\nNote: Chennai Campus "
            "shortlist will be released by chennai campus."
        )
        assert classify_email(subject, body) == "SHORTLIST_UPDATE"

    def test_real_registration_confirmation_still_classifies_application_confirmation(self):
        assert (
            classify_email(
                "Confirmed: Your Registration for TresVista Placement Drive",
                "Registration Confirmed! ... this email confirms your successful "
                "registration for the upcoming placement drive.",
                _CDC_SENDER,
            )
            == "APPLICATION_CONFIRMATION"
        )

    def test_real_single_date_oa_update_still_classifies_oa_update(self):
        assert (
            classify_email(
                "Online Assessment Scheduled",
                "Your online assessment is scheduled for 20-09-2026 by 5.30pm.",
            )
            == "OA_UPDATE"
        )
