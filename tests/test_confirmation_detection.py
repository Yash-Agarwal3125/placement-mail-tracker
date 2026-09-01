"""Unit tests for extraction/confirmation.py (Feature 1, docs/design/10).

All subject/body fixtures below are SYNTHETIC -- docs/design/08-confirmation-
audit.md found zero real CDC confirmation mails anywhere (DB or corpus), so
these are plausible-phrasing guesses the design is defending against, not
observed real mail. Label carried through explicitly per feature_1_spec.
"""

from __future__ import annotations

from placement_mail_tracker.extraction.confirmation import (
    CONFIRMATION_SENDER,
    detect_confirmation_tier,
    extract_company_from_confirmation_subject,
    extract_reference_id,
    find_confident_drive_match,
    is_confirmation_sender,
)


class TestIsConfirmationSender:
    def test_exact_address_matches(self):
        assert is_confirmation_sender(CONFIRMATION_SENDER) is True

    def test_display_name_wrapped_address_matches(self):
        assert is_confirmation_sender(f"CDC Info <{CONFIRMATION_SENDER}>") is True

    def test_case_insensitive(self):
        assert is_confirmation_sender(CONFIRMATION_SENDER.upper()) is True

    def test_unrelated_sender_does_not_match(self):
        assert is_confirmation_sender("cdc@college.edu") is False

    def test_similar_but_different_local_part_does_not_match(self):
        # Guards against a substring-style false positive on a lookalike address.
        assert is_confirmation_sender("noreply.cdcinfo.fake@vitstudent.ac.in") is False


class TestDetectConfirmationTier:  # SYNTHETIC fixtures
    def test_successfully_applied_family(self):
        tier, family = detect_confirmation_tier(
            "Application Update", "You have successfully applied for the Resmed drive."
        )
        assert tier == "CONFIRMED"
        assert family == "successfully_applied_or_registered"

    def test_application_received_family(self):
        tier, family = detect_confirmation_tier(
            "Confirmation", "Your registration has been received for Varroc."
        )
        assert tier == "CONFIRMED"
        assert family == "application_or_registration_received"

    def test_thank_you_for_applying_family(self):
        tier, family = detect_confirmation_tier(
            "Thanks", "Thank you for applying to the Valuelabs drive."
        )
        assert tier == "CONFIRMED"
        assert family == "thank_you_for_applying"

    def test_you_have_applied_family(self):
        tier, family = detect_confirmation_tier(
            "Notice", "You have applied to Infosys Software Engineer."
        )
        assert tier == "CONFIRMED"
        assert family == "you_have_applied"

    def test_html_body_is_tolerated(self):
        tier, family = detect_confirmation_tier(
            "Confirmation",
            "<div><p>You have <b>successfully applied</b> for the role.</p></div>",
        )
        assert tier == "CONFIRMED"
        assert family == "successfully_applied_or_registered"

    def test_unrecognized_phrasing_is_unknown_tier(self):
        tier, family = detect_confirmation_tier(
            "Drive Update", "Please check the portal for the latest status."
        )
        assert tier == "UNKNOWN"
        assert family is None


class TestRealSampleTcsNqt:
    """First real sample (docs/design/10, scripts/eval/corpus/confirmations/
    19f508fea985dbe7.json, fetched 2026-07-13): 'Congratulations! Your TCS
    NQT Application Has Been Successfully Submitted', sender noreply.cdcinfo@
    vitstudent.ac.in. Originally landed UNKNOWN tier / no confident match;
    both gaps were fixed against this real evidence, not guesses."""

    SUBJECT = "Congratulations! Your TCS NQT Application Has Been Successfully Submitted"
    BODY = (
        "Dear Student,\nCongratulations!\nYour application for the TCS National "
        "Qualifier Test (NQT) has been successfully submitted.\nThis is an "
        "important milestone in your placement journey. We encourage you to "
        "start your preparation immediately to maximize your chances of "
        "success.\nTo support your preparation, we will be providing:\n"
        "TCS-specific mock assessments\nRegular practice schedules and "
        "guidance through NeoPAT\nCourse Name:  2027_VIT_TCS NQT Practice "
        "Test\nPractice the assessments in the above course on a regular "
        "basis."
    )

    def test_now_classifies_confirmed_via_broadened_family(self):
        tier, family = detect_confirmation_tier(self.SUBJECT, self.BODY)
        assert tier == "CONFIRMED"
        assert family == "application_or_registration_received"

    def test_no_reference_id_present(self):
        assert extract_reference_id(self.SUBJECT, self.BODY) is None

    def test_matches_tcs_uniquely_despite_ses_substring_collision(self):
        """'ses' is a literal substring of 'assessments' in the body — before
        the short-name word-boundary guard, this tied 100/100 with the real
        TCS match and forced an ambiguous no-match."""
        active_opps = [
            {"drive_id": "TCS_2026_UNKNOWN_ROLE", "company_name": "TCS"},
            {"drive_id": "SES_2026_SUPER_DREAM_INTERNSHIP_INTERN", "company_name": "SES"},
        ]
        match, candidates = find_confident_drive_match(self.SUBJECT, self.BODY, active_opps)
        assert match is not None
        assert match.opportunity["company_name"] == "TCS"
        scores = {c["company_name"]: c["score"] for c in candidates}
        assert scores["SES"] == 0.0


class TestShortCompanyNameMatching:
    def test_short_name_requires_whole_word_not_substring(self):
        opps = [{"drive_id": "SES_2026_INTERN", "company_name": "SES"}]
        match, candidates = find_confident_drive_match(
            "Confirmation", "Your assessments are being processed.", opps,
        )
        assert match is None
        assert candidates[0]["score"] == 0.0

    def test_short_name_whole_word_mention_still_matches(self):
        opps = [{"drive_id": "SES_2026_INTERN", "company_name": "SES"}]
        match, candidates = find_confident_drive_match(
            "Confirmation", "You have successfully applied for SES Internship.", opps,
        )
        assert match is not None
        assert match.opportunity["company_name"] == "SES"


class TestExtractReferenceId:  # SYNTHETIC fixtures
    def test_extracts_id_near_drive_keyword(self):
        assert extract_reference_id("Drive ID: RM-2027-441", "") == "RM-2027-441"

    def test_extracts_id_near_registration_keyword(self):
        assert extract_reference_id("", "Your registration number: REG884211") == "REG884211"

    def test_returns_none_when_absent(self):
        assert extract_reference_id("Application received", "Thanks for applying.") is None

    def test_real_ion_group_sample_extracts_labeled_id_not_a_bare_word(self):
        """Real sample (2026-07-14, message 19f60b9495b5b9c9): the original
        regex made the id/no/number qualifier and the ':' separator both
        optional, so a bare 'drive' anywhere in ordinary prose was itself
        treated as a label with only whitespace before the 'captured' value
        — on this real mail it matched 'Drive' at the end of the subject
        ('...ION Group Placement Drive') and captured 'Placement' from the
        very next line (the body's own opening line), nowhere near the real
        'Drive Number:\\npat-PL-2026-1101' field. Must now return the real ID."""
        subject = "Congratulations! You're Eligible for ION Group Placement Drive"
        body = (
            "Placement Drive Invitation\n"
            "Dear Yash Agarwal,\n"
            "Congratulations! Based on your profile, you are eligible to "
            "participate in the upcoming placement drive with ION Group.\n"
            "Drive Details:\n"
            "Drive Name:\n"
            "ION group\n"
            "Drive Number:\n"
            "pat-PL-2026-1101\n"
            "Date:\n"
            "Company:\n"
            "ION Group\n"
            "Please log in to your placement portal to confirm your "
            "participation and view additional details about the drive."
        )
        assert extract_reference_id(subject, body) == "pat-PL-2026-1101"

    def test_bare_drive_word_in_prose_is_not_treated_as_a_label(self):
        """SYNTHETIC: a mail that mentions 'drive' repeatedly in ordinary
        prose, with no actual Drive Number/Reference/Registration field
        anywhere, must return None rather than a bare-word false positive
        (the exact failure mode the Ion Group sample above demonstrated)."""
        subject = "Update on your placement drive"
        body = (
            "This is regarding the upcoming drive.\n"
            "Please review the drive details before the drive begins.\n"
            "Thank you for your participation in the drive."
        )
        assert extract_reference_id(subject, body) is None


class TestFindConfidentDriveMatch:  # SYNTHETIC fixtures
    def _active_opps(self):
        return [
            {"drive_id": "RESMED_2027_INTERN", "company_name": "Resmed"},
            {"drive_id": "VARROC_2027_FTE", "company_name": "Varroc"},
            {"drive_id": "RESONATE_2027_INTERN", "company_name": "Resonate"},
        ]

    def test_reference_id_exact_match_wins(self):
        opps = self._active_opps()
        match, candidates = find_confident_drive_match(
            "Confirmation", "ref VARROC_2027_FTE", opps, reference_id="VARROC_2027_FTE"
        )
        assert match is not None
        assert match.method == "reference_id"
        assert match.opportunity["company_name"] == "Varroc"
        assert candidates == []

    def test_high_confidence_unique_company_match(self):
        opps = self._active_opps()
        match, candidates = find_confident_drive_match(
            "Application Confirmation",
            "You have successfully applied for Resmed Software Engineer Intern.",
            opps,
        )
        assert match is not None
        assert match.method == "fuzzy_company"
        assert match.opportunity["company_name"] == "Resmed"
        assert candidates  # scored candidates returned even on a confident match

    def test_ambiguous_close_scores_reject_match(self):
        """'Resmed' and 'Resonate' are close enough that a generic confirmation
        body mentioning neither by exact name must not guess between them."""
        opps = self._active_opps()
        match, candidates = find_confident_drive_match(
            "Application Confirmation",
            "Your application has been received for Res.",
            opps,
        )
        assert match is None
        assert len(candidates) == 3

    def test_below_threshold_is_no_match(self):
        opps = self._active_opps()
        match, candidates = find_confident_drive_match(
            "Application Confirmation",
            "Your application has been received.",
            opps,
        )
        assert match is None
        assert candidates

    def test_html_preamble_longer_than_truncation_window_still_matches(self):
        """Strip HTML BEFORE truncating to 500 chars, not after — real
        institutional HTML mail routinely has hundreds of characters of
        <style>/<head> boilerplate before any real content (see the IBM
        Cloud fixture in scripts/eval/corpus/). Truncating the raw HTML
        first can slice off the entire real message, leaving nothing to
        fuzzy-match against even though the company name is right there
        once tags are stripped."""
        opps = self._active_opps()
        html_preamble = "<style>" + ("body{color:red;}" * 60) + "</style>"
        assert len(html_preamble) > 500
        body = (
            f"<html><head>{html_preamble}</head><body>"
            "<p>You have successfully applied for Resmed Software Engineer Intern.</p>"
            "</body></html>"
        )
        match, candidates = find_confident_drive_match("Confirmation", body, opps)
        assert match is not None
        assert match.opportunity["company_name"] == "Resmed"


class TestExtractCompanyFromConfirmationSubject:
    """Phase 6 (calendar-drift remediation plan, Cause 7): these four
    phrasings are the REAL live-Gmail VIT CDC confirmation subjects observed
    in production, unlike the rest of this file's synthetic fixtures."""

    def test_eligible_for_placement_drive(self):
        assert (
            extract_company_from_confirmation_subject(
                "Congratulations! You're Eligible for Honeywell Aerospace Placement Drive"
            )
            == "Honeywell Aerospace"
        )

    def test_confirmed_registration_for_no_suffix(self):
        assert (
            extract_company_from_confirmation_subject(
                "Confirmed: Your Registration for Honeywell Aerospace"
            )
            == "Honeywell Aerospace"
        )

    def test_confirmed_registration_for_placement_drive_suffix(self):
        assert (
            extract_company_from_confirmation_subject(
                "Confirmed: Your Registration for Accenture Placement Drive"
            )
            == "Accenture"
        )

    def test_date_change_for_placement_drive(self):
        assert (
            extract_company_from_confirmation_subject(
                "Important: Date Change for Honeywell Placement Drive"
            )
            == "Honeywell"
        )

    def test_updated_optional_form_available(self):
        assert (
            extract_company_from_confirmation_subject(
                "Updated Optional Form Available - Accenture Drive"
            )
            == "Accenture"
        )

    def test_no_recognized_phrase_returns_none(self):
        assert extract_company_from_confirmation_subject("Application Confirmation") is None
        assert extract_company_from_confirmation_subject("Confirmation") is None

    def test_blank_subject_returns_none(self):
        assert extract_company_from_confirmation_subject("") is None
        assert extract_company_from_confirmation_subject(None) is None

    def test_rejected_junk_token_returns_none(self):
        """docs/design/16 Cause 6/7 interaction: a company candidate that
        normalizes to a rejected junk token must not slip through as a real
        company name just because it followed a recognized confirmation
        phrase."""
        assert (
            extract_company_from_confirmation_subject("Confirmed: Your Registration for Location")
            is None
        )

    def test_subject_fragment_garbage_returns_none(self):
        """This is the collision-magnet incident, reproduced on the
        confirmation path specifically: it shares normalize_company_name's
        rejection with the main extraction path in runner.py, so a subject-
        line fragment like "Is Scheduled On" can't create a garbage drive
        here just because the main path's own gate doesn't run on this
        branch."""
        assert (
            extract_company_from_confirmation_subject(
                "Congratulations! You're Eligible for Is Scheduled On Placement Drive"
            )
            is None
        )
