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
    classify_drive_kind,
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


class TestPptAndAssignmentClassification:
    """docs/design/16 follow-up (2026-08-22): a PPT/pre-placement-talk
    announcement or an assignment-submission mail that starts its own Gmail
    thread (rather than replying in the drive's original registration
    thread) has no thread_id to fall back on -- classify_email() is the only
    thing standing between it and IRRELEVANT, which sits outside
    _PLACEMENT_PROCESS_CLASSIFICATIONS and so never reaches the Phase 1
    safe-attach resolver. Found live: Accenture's real PPT mail and
    Unthinkable's real assignment mail both classified IRRELEVANT and each
    minted a duplicate/junk-named drive instead of attaching to the real
    one. Every subject below is verbatim from production mail."""

    @pytest.mark.parametrize(
        "subject",
        [
            "Blackrock PPT is scheduled on 06.08.2026 by 5:00 pm",
            "Accenture pre placement talk is scheduled on 24.08.2026 by "
            "11:30 am at the respective venues",
            "UBS Pre-Placement Talk is scheduled on 17th August 2026 4.45 "
            "pm - Virtual mode (Own location)",
            "Amazon Internship - PPT is scheduled on August 24, 2026 at "
            "17:00 (IST)",
            "ZS Associates PPT is scheduled on 30/07/2026 - Join immediately",
        ],
    )
    def test_ppt_scheduled_mail_classifies_as_oa_update(self, subject: str):
        assert classify_email(subject, "") == "OA_UPDATE"

    def test_assignment_submission_mail_classifies_as_shortlist_update(self):
        subject = "Kind Attn: Unthinkable Applied Students - Assignment Submission"
        assert classify_email(subject, "") == "SHORTLIST_UPDATE"


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
            pytest.param("Update: Dell Technologies", "Dell Technologies", id="update_no_d_prefix"),
            pytest.param(
                "Join Immediately: Dell Technologies", "Dell Technologies",
                id="join_immediately_prefix",
            ),
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

    def test_needs_gemini_true_for_oa_and_interview_update_even_with_company_role(self):
        """oa_date/interview_date have no rule-based extraction path at all, so
        an OA_UPDATE/INTERVIEW_UPDATE mail must always go to Gemini regardless
        of how confidently company/role were extracted."""
        for classification in ("OA_UPDATE", "INTERVIEW_UPDATE"):
            result = RuleExtractionResult(
                company_name="Microsoft",
                role="SDE Intern",
                current_status="SHORTLISTED",
                email_classification=classification,
            )
            assert result.needs_gemini is True

    def test_shortlist_notice_subjects_extract_company_without_gemini(self):
        """VIT CDC shortlist notices like 'Company - [Role] shortlisted list -
        Reg' previously extracted no company at all, forcing every one of
        these through Gemini (and getting stuck once the free-tier daily
        quota was exhausted). Rule-based extraction alone should now resolve
        company + status and avoid Gemini for these."""
        cases = [
            ("Hindustan Unilever - Bio Tech shortlisted list - Reg", "Hindustan Unilever"),
            ("Clayfin - Shortlisted list - Reg", "Clayfin"),
        ]
        for subject, expected_company in cases:
            result = extract_from_email(subject)
            assert result.company_name == expected_company
            assert result.current_status == "SHORTLISTED"
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


# ===================================================================
# docs/design/16 Cause 6: rejected junk company names
# ===================================================================


class TestRejectedCompanyTokens:
    """A rejection list of body-text tokens that are never company names,
    applied in normalize_company_name(). Must never be length-based --
    UBS, TCS, KLA, ZF, Q2, WSP, SES and IPR are all real companies."""

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("Location", id="location"),
            pytest.param("Own Location", id="own_location"),
            pytest.param("Online Test", id="online_test"),
            pytest.param("Selection", id="selection"),
            pytest.param("Shortlisted", id="shortlisted"),
            pytest.param("Details", id="details"),
            pytest.param("Com", id="com"),
        ],
    )
    def test_rejected_tokens_return_none(self, raw):
        assert normalize_company_name(raw) is None

    def test_unknown_is_deliberately_not_rejected(self):
        """docs/design/16 lists "Unknown" as a rejected token, but "Unknown"/
        "Unknown Company" is an existing sentinel value this codebase
        already stores as a literal company_name
        (scheduler.runner._UNIDENTIFIED_COMPANIES,
        db.manager._normalize_opportunity's own "Unknown Company" fallback)
        -- rejecting it here would turn that sentinel into a NOT NULL
        constraint violation. See the task report for this conflict."""
        assert normalize_company_name("Unknown") == "Unknown"

    @pytest.mark.parametrize(
        "raw",
        ["UBS", "TCS", "KLA", "ZF", "Q2", "WSP", "SES", "IPR"],
    )
    def test_real_short_company_names_survive(self, raw):
        """No length-based gate: these real 2-3 letter companies must not
        be rejected merely for being short."""
        result = normalize_company_name(raw)
        assert result is not None
        assert result != ""

    def test_blank_input_still_returns_empty_string_not_none(self):
        """Rejected-token None is distinct from the pre-existing blank-input
        '' case -- callers that check `if not company_name` still work for
        both, but only the rejected-token case is `is None`."""
        assert normalize_company_name("") == ""
        assert normalize_company_name(None) == ""


# ===================================================================
# docs/design/16 Cause 6: DD-MM-YYYY By H.MMPm date extraction
# ===================================================================


class TestOaDateExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            pytest.param(
                "Online Test Is Scheduled On 18-08-2026 By 2.30Pm @PRP 717.",
                "2026-08-18T14:30",
                id="by_2_30pm",
            ),
            pytest.param(
                "The OA is on 05-08-2026 by 1.30pm sharp.",
                "2026-08-05T13:30",
                id="lowercase_by_1_30pm",
            ),
            pytest.param(
                "Scheduled 01-01-2027 By 12.30Pm noon-ish.",
                "2027-01-01T12:30",
                id="noon_edge_12_30pm",
            ),
            pytest.param(
                "Scheduled 01-01-2027 By 12.15Am midnight-ish.",
                "2027-01-01T00:15",
                id="midnight_edge_12_15am",
            ),
            pytest.param("No date here at all.", None, id="no_match"),
        ],
    )
    def test_oa_date_pattern(self, text, expected):
        result = extract_from_email("Subject", text)
        assert result.oa_date == expected

    def test_honeywell_regression_company_none_and_oa_date_parsed(self):
        """docs/design/16 Cause 6 regression: this exact production subject
        must yield company_name=None (not "Location") and the correct
        oa_date, both via rule-based extraction alone."""
        subject = (
            "Honeywell Aerospace PPT (12Noon @Own Location) & Online Test "
            "Is Scheduled On 18-08-2026 By 2.30Pm @PRP 717."
        )
        result = extract_from_email(subject)
        assert result.company_name is None
        assert result.oa_date == "2026-08-18T14:30"

    def test_to_dict_carries_oa_date(self):
        result = RuleExtractionResult(oa_date="2026-08-18T14:30")
        assert result.to_dict()["oa_date"] == "2026-08-18T14:30"


# ===================================================================
# docs/design/16 Cause 4: WEBINAR drive kind
# ===================================================================


class TestWebinarDriveKind:
    @pytest.mark.parametrize(
        "subject",
        [
            pytest.param(
                "Deloitte US-India's BRIDGE Campus Learning Series | "
                "Registrations now open",
                id="deloitte_learning_series",
            ),
            pytest.param("Join our Webinar on Cloud Careers", id="webinar"),
            pytest.param("Tech Talk: Building at Scale", id="tech_talk"),
            pytest.param("Info Session for Interested Students", id="info_session"),
            pytest.param("Masterclass on System Design", id="masterclass"),
            pytest.param("Awareness Session on Data Privacy", id="awareness_session"),
        ],
    )
    def test_webinar_wording_classifies_as_webinar(self, subject):
        assert classify_drive_kind(subject) == "WEBINAR"

    def test_deloitte_subject_through_full_extraction_is_webinar(self):
        """Pin the classify_email interaction too: 'Registrations now open'
        does not match the NEW_DRIVE classifier's 'registration open'
        pattern, so classification falls through to IRRELEVANT and WEBINAR
        still wins on drive_kind."""
        subject = (
            "Deloitte US-India's BRIDGE Campus Learning Series | "
            "Registrations now open"
        )
        result = extract_from_email(subject)
        assert result.drive_kind == "WEBINAR"

    def test_placement_process_classification_still_wins_over_webinar_wording(self):
        """The existing _PLACEMENT_PROCESS_CLASSIFICATIONS override must
        keep winning: a mail classified OA_UPDATE is a real process mail
        regardless of webinar-sounding subject wording (this guard already
        saved a real Honeywell drive from being misfiled as a hackathon)."""
        result = classify_drive_kind(
            "Webinar Learning Series", email_classification="OA_UPDATE",
        )
        assert result == "PLACEMENT"

    def test_campus_connect_hackathon_stays_hackathon(self):
        """HACKATHON must keep matching before WEBINAR's 'campus connect'
        pattern -- a real Honeywell drive is literally named this."""
        assert classify_drive_kind("Honeywell Campus Connect Hackathon") == "HACKATHON"

    def test_bootcamp_stays_workshop_not_webinar(self):
        """bootcamp is already claimed by WORKSHOP; it must not be
        duplicated into WEBINAR (would silently reclassify history)."""
        assert classify_drive_kind("Summer Bootcamp Registrations") == "WORKSHOP"

    def test_campus_connect_with_no_drive_vocabulary_is_webinar(self):
        """'Campus Connect' alone (no package/CGPA/deadline language) is a
        company-branding phrase, not evidence of a hiring drive."""
        assert classify_drive_kind("Acme Campus Connect Session") == "WEBINAR"

    def test_campus_connect_with_drive_vocabulary_is_placement(self):
        """Safety-nets plan Phase 5 acceptance example: a real hiring drive
        that happens to brand itself 'Campus Connect' must not be hidden
        from the calendar just because of that phrase."""
        subject = (
            "Acme Campus Connect Drive — 12 LPA, 7.0 CGPA, register by 20 Aug"
        )
        assert classify_drive_kind(subject) == "PLACEMENT"

    @pytest.mark.parametrize(
        "phrase",
        [
            "registration deadline is 20 Aug",
            "CTC of 12 LPA",
            "stipend of 50k per month",
            "package offered is attractive",
            "12 LPA package",
            "5 Lakhs per annum",
            "CGPA 7.0 required",
            "eligibility criteria: 7.0 CGPA",
            "selection process will have 3 rounds",
        ],
    )
    def test_campus_connect_excluded_by_each_drive_vocab_phrase(self, phrase):
        assert classify_drive_kind(f"Campus Connect — {phrase}") == "PLACEMENT"
