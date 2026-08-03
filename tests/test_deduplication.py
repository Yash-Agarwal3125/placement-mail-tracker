"""Regression tests for docs/design/15-roster-verification-and-confirmation-trust.md §1.

Covers the duplicate-drive fix (§1.4 A-D): a missing/empty field must never be
scored as a mismatch, ``internship_and_fulltime`` must be compatible with both
``internship`` and ``full_time``, company normalisation must route through the
richer ``rule_engine.normalize_company_name``, the first-character candidate
prefilter must be gone, and a pair that passes identity but lacks event
coincidence must be routed to review rather than silently merged or inserted.

Fixtures for the Cloudsek/Tube Products/VIT pairs are reconstructed from the
live DB rows cited in the design doc (ids 1, 6, 21, 33, 69, 79).
"""

from __future__ import annotations

from placement_mail_tracker.utils.deduplication import (
    DeduplicationConfig,
    compare_opportunities,
    find_all_matches,
    find_best_match,
    find_review_matches,
    normalize_company,
    types_compatible,
)

# ---------------------------------------------------------------------------
# Fixtures reconstructed from the live DB (doc 15 §1.1, §1.3)
# ---------------------------------------------------------------------------

CLOUDSEK_1 = {
    "id": 1,
    "company_name": "Cloudsek",
    "role": "Unknown Role",
    "internship_or_fulltime": None,
    "oa_date": "2026-07-01T15:00",
    "interview_date": None,
    "deadline": None,
    "source_thread_id": "19f188e3310fe90f",
}
CLOUDSEK_6 = {
    "id": 6,
    "company_name": "Cloudsek",
    "role": "Unknown Role",
    "internship_or_fulltime": "fulltime",
    "oa_date": "2026-07-01T14:00",
    "interview_date": None,
    "deadline": None,
    "source_thread_id": "19f1959722431ada",
}

TUBE_PRODUCTS_21 = {
    "id": 21,
    "company_name": "Tube Products Of",
    "role": "Unknown Role",
    "internship_or_fulltime": None,
    "oa_date": None,
    "interview_date": "2026-07-07T13:00",
    "deadline": None,
    "source_thread_id": "19f26410d9120880",
}
TUBE_PRODUCTS_33 = {
    "id": 33,
    "company_name": "Tube Products Of",
    "role": "Unknown Role",
    "internship_or_fulltime": "fulltime",
    "oa_date": None,
    "interview_date": "2026-07-07T13:00",
    "deadline": None,
    "source_thread_id": "19f365da012dd957",
}

# The required negative case (doc 15 §1.4-D, §6.2): two genuinely different
# VIT drives that score high on company similarity alone and must never merge.
VIT_69 = {
    "id": 69,
    "company_name": "Vellore Institute Of (vit)",
    "role": "Student Tech Team Member",
    "internship_or_fulltime": None,
    "oa_date": None,
    "interview_date": None,
    "deadline": "2026-07-30",
    "source_thread_id": "19f9887b1aa1f06e",
}
VIT_79 = {
    "id": 79,
    "company_name": "Vellore Institute Of (vit)",
    "role": "Student Information Session on SAP (Semester Abroad Program)",
    "internship_or_fulltime": None,
    "oa_date": None,
    "interview_date": "2026-07-29T11:30",
    "deadline": None,
    "source_thread_id": "19f93779192dff47",
}


class TestModeAAndTubeProductsMerge:
    """Doc 15 §1.4-A: missing internship_or_fulltime must not block a merge."""

    def test_cloudsek_pair_merges(self):
        result = compare_opportunities(CLOUDSEK_1, CLOUDSEK_6)
        assert result.is_duplicate is True
        assert result.blocked_reason is None
        # Confidence arithmetic assertion per doc §6.2 -- not just the boolean.
        assert result.confidence_score == 100.0
        assert result.type_score.is_absent is True

    def test_tube_products_pair_merges(self):
        result = compare_opportunities(TUBE_PRODUCTS_21, TUBE_PRODUCTS_33)
        assert result.is_duplicate is True
        assert result.blocked_reason is None
        assert result.confidence_score == 100.0

    def test_cloudsek_via_find_best_match(self):
        best = find_best_match(CLOUDSEK_1, [CLOUDSEK_6, VIT_69, VIT_79])
        assert best is not None
        assert best.is_duplicate is True
        assert best.candidate_id == 6

    def test_old_behaviour_regression_guard_type_not_scored_as_zero(self):
        """The doc's headline bug: a NULL type must not drag confidence to 81.99."""
        result = compare_opportunities(CLOUDSEK_1, CLOUDSEK_6)
        assert result.confidence_score > 82.0


class TestVitNegativeCase:
    """Doc 15 §1.4-D, §6.2: ids 69/79 are different drives and must not merge."""

    def test_vit_pair_does_not_merge(self):
        result = compare_opportunities(VIT_69, VIT_79)
        assert result.is_duplicate is False
        assert result.blocked_reason == "role_conflict"

    def test_vit_pair_not_found_via_find_best_match(self):
        best = find_best_match(VIT_69, [VIT_79])
        # Either None, or a review candidate -- never a merge.
        assert best is None or best.is_duplicate is False

    def test_vit_pair_not_in_find_all_matches(self):
        matches = find_all_matches(VIT_69, [VIT_79, CLOUDSEK_1, CLOUDSEK_6])
        assert all(m.candidate_id != 79 for m in matches)


class TestInternshipVsFullTimeBlocker:
    """Doc 15 §1.4-D blocker: known-and-different type at the same company/date
    must never merge, even with identical company and role and a coincident date.
    """

    INTERN = {
        "id": 200,
        "company_name": "Acme Corp",
        "role": "Software Engineer",
        "internship_or_fulltime": "internship",
        "oa_date": "2026-08-01T10:00",
        "interview_date": None,
        "deadline": None,
        "source_thread_id": "thread_intern",
    }
    FULLTIME = {
        "id": 201,
        "company_name": "Acme Corp",
        "role": "Software Engineer",
        "internship_or_fulltime": "fulltime",
        "oa_date": "2026-08-01T10:00",
        "interview_date": None,
        "deadline": None,
        "source_thread_id": "thread_fulltime",
    }

    def test_internship_vs_fulltime_same_company_same_date_does_not_merge(self):
        result = compare_opportunities(self.INTERN, self.FULLTIME)
        assert result.is_duplicate is False
        assert result.blocked_reason == "type_conflict"

    def test_internship_vs_fulltime_via_find_best_match(self):
        best = find_best_match(self.INTERN, [self.FULLTIME])
        assert best is None or best.is_duplicate is False


class TestInternshipAndFulltimeCompatibility:
    """Doc 15 §1.4-D: internship_and_fulltime is compatible with both, not a
    third, mutually-exclusive value.
    """

    def test_types_compatible_helper(self):
        assert types_compatible("internship_and_fulltime", "internship") is True
        assert types_compatible("internship_and_fulltime", "full_time") is True
        assert types_compatible("internship_and_fulltime", "internship_and_fulltime") is True
        assert types_compatible("internship", "full_time") is False

    def test_internship_and_fulltime_merges_with_internship(self):
        a = dict(TestInternshipVsFullTimeBlocker.INTERN)
        b = dict(TestInternshipVsFullTimeBlocker.INTERN)
        b["id"] = 202
        b["internship_or_fulltime"] = "internship_and_fulltime"
        result = compare_opportunities(a, b)
        assert result.is_duplicate is True
        assert result.blocked_reason is None

    def test_internship_and_fulltime_merges_with_fulltime(self):
        a = dict(TestInternshipVsFullTimeBlocker.FULLTIME)
        b = dict(TestInternshipVsFullTimeBlocker.FULLTIME)
        b["id"] = 203
        b["internship_or_fulltime"] = "internship_and_fulltime"
        result = compare_opportunities(a, b)
        assert result.is_duplicate is True
        assert result.blocked_reason is None


class TestCompanyNormalizationRoutesThroughRuleEngine:
    """Doc 15 §1.4-C."""

    def test_strips_label_prefix(self):
        assert normalize_company("Update: Varroc Engineering") == normalize_company(
            "Varroc Engineering"
        )
        assert normalize_company("Join Immediately: Varroc Engineering") == normalize_company(
            "Varroc Engineering"
        )

    def test_eternal_zomato_alias(self):
        assert normalize_company("Eternal (zomato)") == "Zomato"
        assert normalize_company("Zomato") == "Zomato"

    def test_varroc_variants_merge(self):
        a = {"id": 41, "company_name": "Varroc Engineering", "role": "Unknown Role",
             "internship_or_fulltime": None}
        b = {"id": 44, "company_name": "Join Immediately: Varroc Engineering",
             "role": "Unknown Role", "internship_or_fulltime": None}
        result = compare_opportunities(a, b)
        assert result.company_score.exact_match is True
        assert result.is_duplicate is True

    def test_eternal_zomato_vs_zomato_now_comparable(self):
        """Doc 15 §1.4-C: the first-character prefilter used to make this
        pair (E vs Z) impossible to even compare.
        """
        zomato = {"id": 67, "company_name": "Zomato", "role": "Unknown Role",
                  "internship_or_fulltime": None, "oa_date": "2026-07-26T11:30"}
        eternal = {"id": 66, "company_name": "Eternal (zomato)", "role": "Unknown Role",
                   "internship_or_fulltime": None, "oa_date": "2026-07-26T11:30"}
        result = compare_opportunities(zomato, eternal)
        assert result.company_score.exact_match is True
        assert result.is_duplicate is True


class TestFirstCharacterPrefilterRemoved:
    """Doc 15 §1.4-C: find_all_matches must compare every candidate, not just
    those sharing the first character of the normalised company name.
    """

    def test_dissimilar_first_letters_are_still_compared(self):
        zomato = {"id": 67, "company_name": "Zomato", "role": "Unknown Role",
                  "internship_or_fulltime": None, "oa_date": "2026-07-26T11:30"}
        eternal = {"id": 66, "company_name": "Eternal (zomato)", "role": "Unknown Role",
                   "internship_or_fulltime": None, "oa_date": "2026-07-26T11:30"}
        matches = find_all_matches(zomato, [eternal])
        assert len(matches) == 1
        assert matches[0].candidate_id == 66


class TestReviewRouting:
    """Doc 15 §1.4-D stage 2: identity without event coincidence -> review,
    never a silent merge and never (from the caller's perspective) a silent
    insert.
    """

    def test_identity_match_without_coincidence_is_flagged_for_review(self):
        incoming = {
            "id": None,
            "company_name": "Globex Corp",
            "role": "Data Analyst",
            "internship_or_fulltime": "internship",
            "oa_date": None,
            "interview_date": None,
            "deadline": "2026-09-01T09:00",
            "source_thread_id": "thread_new",
        }
        candidate = {
            "id": 500,
            "company_name": "Globex Corp",
            "role": "Data Analyst",
            "internship_or_fulltime": "internship",
            "oa_date": None,
            "interview_date": None,
            "deadline": "2026-11-15T09:00",  # >6h away, no shared thread
            "source_thread_id": "thread_old",
        }
        result = compare_opportunities(incoming, candidate)
        assert result.is_duplicate is False
        assert result.review_required is True
        assert result.blocked_reason is None

        reviews = find_review_matches(incoming, [candidate])
        assert len(reviews) == 1
        assert reviews[0].candidate_id == 500

    def test_first_oa_notice_fills_empty_field_without_manufactured_review(self):
        """Regression: a drive that already has a deadline (nearly all of
        them) but no oa_date yet must accept its first OA notice even
        without a shared Gmail thread -- there is no existing oa_date for
        the new one to contradict. Previously this always routed to review
        because `deadline` being populated on the candidate made
        `no_date_evidence` False, while the mismatched thread ids and the
        (both-None) oa_date/interview_date fields left `coincidence` False
        too -- the very first OA/interview email for *any* drive could
        never be applied.
        """
        incoming = {
            "id": None,
            "company_name": "Tekion",
            "role": None,
            "internship_or_fulltime": None,
            "oa_date": "2026-07-28T09:30",
            "interview_date": None,
            "deadline": None,
            "source_thread_id": "thread_oa_notice",
        }
        candidate = {
            "id": 89,
            "company_name": "Tekion",
            "role": None,
            "internship_or_fulltime": None,
            "oa_date": None,
            "interview_date": None,
            "deadline": "2026-07-24T09:00",
            "source_thread_id": "thread_original_drive",
        }
        result = compare_opportunities(incoming, candidate)
        assert result.is_duplicate is True
        assert result.review_required is False

    def test_dateless_status_update_merges_against_dated_candidate(self):
        """Regression: a rejection/eligibility/registration-confirmation mail
        never carries oa_date/interview_date/deadline -- it just states a
        status against an already-tracked company+role. Previously this was
        forced to review whenever the *candidate* had any date populated
        (nearly all drives do, via `deadline` from the original
        announcement), so these status-only corrections could never actually
        update `current_status` -- they piled up in unmatched_confirmations
        forever, leaving stale/optimistic statuses (and their calendar
        events) in place after a real rejection or ineligibility mail.
        """
        incoming = {
            "id": None,
            "company_name": "Zluri",
            "role": "SDE Intern",
            "internship_or_fulltime": "internship",
            "oa_date": None,
            "interview_date": None,
            "deadline": None,
            "current_status": "REJECTED",
            "source_thread_id": "thread_rejection",
        }
        candidate = {
            "id": 700,
            "company_name": "Zluri",
            "role": "SDE Intern",
            "internship_or_fulltime": "internship",
            "oa_date": "2026-06-01T09:00",
            "interview_date": None,
            "deadline": "2026-05-20T09:00",
            "source_thread_id": "thread_original_drive",
        }
        result = compare_opportunities(incoming, candidate)
        assert result.is_duplicate is True
        assert result.review_required is False

    def test_dateless_identity_match_merges_without_manufactured_review(self):
        """When neither side has any date field populated at all, there is
        nothing to contradict identity with -- must not be forced to review.
        """
        incoming = {
            "id": None,
            "company_name": "Initech",
            "role": "Backend Engineer",
            "internship_or_fulltime": "internship",
            "oa_date": None,
            "interview_date": None,
            "deadline": None,
            "source_thread_id": "thread_new",
        }
        candidate = {
            "id": 600,
            "company_name": "Initech",
            "role": "Backend Engineer",
            "internship_or_fulltime": "internship",
            "oa_date": None,
            "interview_date": None,
            "deadline": None,
            "source_thread_id": "thread_old",
        }
        result = compare_opportunities(incoming, candidate)
        assert result.is_duplicate is True
        assert result.review_required is False


class TestConfig:
    def test_weights_still_validated(self):
        import pytest

        with pytest.raises(ValueError):
            DeduplicationConfig(company_weight=0.5, role_weight=0.5, type_weight=0.5)
