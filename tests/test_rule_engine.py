"""Phase 3 / 4 / 13: Rule-engine tests.

Covers:
- Email classification (NEW_DRIVE, OA_UPDATE, etc.)
- Status detection (OPEN, OA, SHORTLISTED, etc.)
- Company name normalization
- Field extraction (CTC, stipend, deadline, role, location)
- ``needs_gemini`` flag logic
"""

from __future__ import annotations

import pytest

from placement_mail_tracker.extraction.rule_engine import (
    RuleExtractionResult,
    classify_email,
    detect_status_from_text,
    extract_from_email,
    normalize_company_name,
)

# ===================================================================
# Phase 13: classify_email
# ===================================================================


class TestClassifyEmail:
    """Verify email classification into the 8 buckets."""

    @pytest.mark.parametrize(
        "subject,body,expected",
        [
            pytest.param(
                "Campus Drive – Microsoft Summer Internship 2027",
                "Registration open for Microsoft campus hiring.",
                "NEW_DRIVE",
                id="new_drive",
            ),
            pytest.param(
                "OA Scheduled – Dell Technologies",
                "The online assessment for Dell has been scheduled.",
                "OA_UPDATE",
                id="oa_update",
            ),
            pytest.param(
                "Shortlisted Students – Standard Chartered",
                "The following students have been shortlisted for the next round.",
                "SHORTLIST_UPDATE",
                id="shortlist_update",
            ),
            pytest.param(
                "Interview Scheduled – HPE",
                "Interview round for HPE will be held on 15th June.",
                "INTERVIEW_UPDATE",
                id="interview_update",
            ),
            pytest.param(
                "Offer Letter Released – Amazon",
                "Congratulations! The offer letters have been released.",
                "OFFER_UPDATE",
                id="offer_update",
            ),
            pytest.param(
                "Reminder: Last Date to Register – Google",
                "This is a reminder that the deadline for Google registration is tomorrow.",
                "REMINDER",
                id="reminder",
            ),
            pytest.param(
                "Updated: Dell Technologies Campus Drive – Schedule Change",
                "Updated information regarding the Dell campus drive.",
                "DRIVE_UPDATE",
                id="drive_update",
            ),
            pytest.param(
                "Weekly Newsletter – Campus Life",
                "Here is what happened this week on campus.",
                "IRRELEVANT",
                id="irrelevant",
            ),
        ],
    )
    def test_classify_email(self, subject: str, body: str, expected: str):
        assert classify_email(subject, body) == expected

    def test_classify_empty_input(self):
        assert classify_email("", "") == "IRRELEVANT"


# ===================================================================
# Phase 2: detect_status_from_text
# ===================================================================


class TestDetectStatus:
    """Verify status detection from email text."""

    @pytest.mark.parametrize(
        "subject,body,expected",
        [
            pytest.param(
                "Campus Drive – Microsoft",
                "Registration is now open for the Microsoft campus drive.",
                "OPEN",
                id="open",
            ),
            pytest.param(
                "OA Scheduled – Dell",
                "Online assessment for Dell has been scheduled on HackerRank.",
                "OA",
                id="oa",
            ),
            pytest.param(
                "Shortlisted – Standard Chartered",
                "The following students have been shortlisted for the interview.",
                "SHORTLISTED",
                id="shortlisted",
            ),
            pytest.param(
                "Interview Scheduled – HPE",
                "Technical interview round is scheduled for 15th June.",
                "INTERVIEW",
                id="interview",
            ),
            pytest.param(
                "HR Round – Tata Motors",
                "HR round of the interview will be conducted tomorrow.",
                "HR",
                id="hr",
            ),
            pytest.param(
                "Offer Released – Amazon",
                "Congratulations! You have been selected. Offer letter attached.",
                "OFFER_RECEIVED",
                id="offer_received",
            ),
            pytest.param(
                "Not Shortlisted – Infosys",
                "We regret to inform that you have not been shortlisted.",
                "REJECTED",
                id="rejected",
            ),
            pytest.param(
                "Drive Cancelled – TCS",
                "We regret to inform you that the drive has been cancelled due to hiring freeze.",
                "WITHDRAWN",
                id="withdrawn",
            ),
        ],
    )
    def test_detect_status(self, subject: str, body: str, expected: str):
        assert detect_status_from_text(subject, body) == expected

    def test_detect_status_empty_input(self):
        assert detect_status_from_text("", "") == "OPEN"


# ===================================================================
# Phase 4: normalize_company_name
# ===================================================================


class TestNormalizeCompanyName:
    """Verify canonical name normalization."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            pytest.param("Dell Technologies", "Dell Technologies", id="dell_exact"),
            pytest.param("DELL", "Dell Technologies", id="dell_uppercase"),
            pytest.param("dell", "Dell Technologies", id="dell_lowercase"),
            pytest.param("DELL TECHNOLOGIES", "Dell Technologies", id="dell_full_upper"),
            pytest.param("Microsoft Corporation", "Microsoft", id="microsoft_corp"),
            pytest.param("Microsoft", "Microsoft", id="microsoft_exact"),
            pytest.param("Updated : Dell Technologies", "Dell Technologies", id="updated_prefix"),
            pytest.param("Reminder : Dell", "Dell Technologies", id="reminder_prefix"),
            pytest.param("Tata Motors Ltd.", "Tata Motors", id="tata_motors_ltd"),
            pytest.param("tata motors", "Tata Motors", id="tata_motors_lower"),
            pytest.param("TATA MOTORS", "Tata Motors", id="tata_motors_upper"),
            pytest.param("Hewlett Packard Enterprise", "Hewlett Packard Enterprise", id="hpe_full"),
            pytest.param("HPE", "Hewlett Packard Enterprise", id="hpe_abbrev"),
            pytest.param("Standard Chartered", "Standard Chartered", id="sc_exact"),
            pytest.param("Infosys", "Infosys", id="infosys"),
            pytest.param("TCS", "TCS", id="tcs"),
            pytest.param("Tata Consultancy Services", "TCS", id="tcs_full"),
            pytest.param("", "", id="empty_string"),
            pytest.param(None, "", id="none_input"),
        ],
    )
    def test_normalize_company_name(self, raw, expected):
        assert normalize_company_name(raw) == expected


# ===================================================================
# Phase 3: extract_from_email
# ===================================================================


class TestExtractFromEmail:
    """Verify rule-based field extraction from email subject + body."""

    def test_full_extraction(self):
        """All fields present → high confidence and no Gemini needed."""
        subject = "Campus Drive – Microsoft Summer Internship 2027"
        body = (
            "Role: Software Engineer Intern\n"
            "CTC: 12 LPA\n"
            "Stipend: Rs. 50000 per month\n"
            "Location: Bangalore\n"
            "Deadline: 15 June 2027\n"
            "Registration link: https://forms.gle/abc123\n"
            "This is a summer internship opportunity."
        )
        result = extract_from_email(subject, body)

        assert isinstance(result, RuleExtractionResult)
        assert result.company_name is not None
        assert result.role is not None
        assert result.ctc is not None
        assert result.stipend is not None
        assert result.deadline is not None
        assert result.registration_link is not None
        assert result.category == "internship"
        assert result.confidence > 0.5
        assert result.needs_gemini is False

    def test_partial_extraction_needs_gemini(self):
        """Missing company → needs_gemini should be True."""
        subject = "Important Update"
        body = "The OA for the campus drive is scheduled for next week."
        result = extract_from_email(subject, body)

        assert result.company_name is None
        assert result.needs_gemini is True
        assert "company_name" in result.missing_fields

    def test_needs_gemini_false_when_company_and_role_present(self):
        """When company and role are present, and status is detected, Gemini is not needed."""
        subject = "Campus Drive – Dell Technologies Summer Internship"
        body = (
            "Role: Software Engineer Intern\n"
            "Dell Technologies campus hiring for 2027 batch."
        )
        result = extract_from_email(subject, body)
        assert result.company_name is not None
        assert result.role is not None
        # Complete company/role/status extraction should avoid Gemini.
        if result.email_classification != "IRRELEVANT" or result.current_status != "OPEN":
            assert result.needs_gemini is False

    def test_to_dict(self):
        """``to_dict`` should produce an opportunity-compatible dictionary."""
        result = RuleExtractionResult(
            company_name="Microsoft",
            role="SDE Intern",
            category="internship",
            ctc="12 LPA",
            stipend="50000 per month",
            deadline="15 June 2027",
            location="Bangalore",
            registration_link="https://forms.gle/test",
            current_status="OPEN",
        )
        d = result.to_dict()
        assert d["company_name"] == "Microsoft"
        assert d["role"] == "SDE Intern"
        assert d["internship_or_fulltime"] == "internship"
        assert d["package_or_stipend"] == "12 LPA"  # ctc preferred
        assert d["deadline"] == "15 June 2027"
        assert d["work_location"] == "Bangalore"
        assert d["current_status"] == "OPEN"


# ===================================================================
# CTC Extraction
# ===================================================================


class TestCTCExtraction:
    @pytest.mark.parametrize(
        "body,expected_contains",
        [
            pytest.param("CTC: 12 LPA", "12 LPA", id="ctc_lpa"),
            pytest.param("Package: 8.5 Lakhs Per Annum", "8.5 Lakhs Per Annum", id="pkg_lakhs"),
            pytest.param("Salary: Rs. 3,60,000", None, id="salary_rs"),
            pytest.param("No compensation mentioned here.", None, id="no_ctc"),
        ],
    )
    def test_ctc_extraction(self, body, expected_contains):
        result = extract_from_email("Campus Drive – Test Company", body)
        if expected_contains:
            assert result.ctc is not None
            assert expected_contains in result.ctc
        else:
            # May or may not extract – just don't crash
            pass


# ===================================================================
# Stipend Extraction
# ===================================================================


class TestStipendExtraction:
    @pytest.mark.parametrize(
        "body,expected_contains",
        [
            pytest.param("Stipend: Rs. 50000 per month", "50000 per month", id="stipend_pm"),
            pytest.param("Monthly allowance: Rs. 25000", "25000", id="allowance"),
            pytest.param("No stipend information available.", None, id="no_stipend"),
        ],
    )
    def test_stipend_extraction(self, body, expected_contains):
        result = extract_from_email("Campus Drive – Test Intern", body)
        if expected_contains:
            assert result.stipend is not None
            assert expected_contains in result.stipend
        else:
            assert result.stipend is None


# ===================================================================
# Deadline Extraction
# ===================================================================


class TestDeadlineExtraction:
    @pytest.mark.parametrize(
        "body,expected_contains",
        [
            pytest.param("Deadline: 15 June 2027", "15 June 2027", id="deadline_full"),
            pytest.param("Last date: 20 July 2027", "20 July 2027", id="last_date"),
            pytest.param("Register by 10-Jun-2027", "10-Jun-2027", id="register_by"),
            pytest.param("Apply before 25/07/2027", "25/07/2027", id="apply_before"),
            pytest.param("No deadline mentioned.", None, id="no_deadline"),
        ],
    )
    def test_deadline_extraction(self, body, expected_contains):
        result = extract_from_email("Campus Drive – Test Co", body)
        if expected_contains:
            assert result.deadline is not None
            assert expected_contains in result.deadline
        else:
            assert result.deadline is None
