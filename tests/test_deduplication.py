"""Tests for utils/deduplication.py."""

from __future__ import annotations

import pytest

from placement_mail_tracker.utils.deduplication import (
    FIELD_COMPANY,
    FIELD_ROLE,
    FIELD_TYPE,
    DeduplicationConfig,
    DuplicateResult,
    FieldScore,
    UpdatedField,
    compare_opportunities,
    compute_confidence_score,
    detect_updates,
    exact_match,
    exact_match_fields,
    find_all_matches,
    find_best_match,
    find_duplicates_in_list,
    fuzzy_score_company,
    fuzzy_score_role,
    fuzzy_score_type,
    is_duplicate,
    normalize_company,
    normalize_opportunity_type,
    normalize_text,
    rapidfuzz_prefilter,
    score_company,
    score_role,
    score_type,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def default_config() -> DeduplicationConfig:
    return DeduplicationConfig()


def _opp(
    company: str | None = "Google",
    role: str | None = "Software Engineer",
    opp_type: str | None = "internship",
    **extra,
) -> dict:
    return {
        FIELD_COMPANY: company,
        FIELD_ROLE: role,
        FIELD_TYPE: opp_type,
        "id": extra.pop("id", 1),
        "package_or_stipend": extra.pop("package_or_stipend", None),
        "deadline": extra.pop("deadline", None),
        **extra,
    }


# ---------------------------------------------------------------------------
# normalize_text
# ---------------------------------------------------------------------------


class TestNormalizeText:
    def test_lowercases(self):
        assert normalize_text("Hello World") == "hello world"

    def test_strips_whitespace(self):
        assert normalize_text("  foo bar  ") == "foo bar"

    def test_collapses_internal_spaces(self):
        assert normalize_text("foo   bar") == "foo bar"

    def test_removes_punctuation(self):
        # Punctuation is replaced by spaces, then all whitespace is collapsed.
        assert normalize_text("foo, bar! baz.") == "foo bar baz"
        assert normalize_text("foo.") == "foo"

    def test_none_returns_empty_string(self):
        assert normalize_text(None) == ""

    def test_empty_string_returns_empty_string(self):
        assert normalize_text("") == ""

    def test_unicode_nfkc(self):
        # Full-width capital A → normal A
        assert normalize_text("\uff21") == "a"


# ---------------------------------------------------------------------------
# normalize_company
# ---------------------------------------------------------------------------


class TestNormalizeCompany:
    def test_strips_pvt_ltd(self):
        assert normalize_company("Acme Pvt. Ltd.") == "acme"

    def test_strips_technologies(self):
        assert normalize_company("FooBar Technologies") == "foobar"

    def test_preserves_core_name(self):
        assert normalize_company("Google") == "google"

    def test_multiple_suffix_tokens(self):
        result = normalize_company("Infosys Technologies Limited India")
        assert result == "infosys"

    def test_none_returns_empty_string(self):
        assert normalize_company(None) == ""

    def test_all_suffix_tokens_preserves_original(self):
        # If all tokens are suffixes, return the (stripped) base rather than empty
        result = normalize_company("Ltd.")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# normalize_opportunity_type
# ---------------------------------------------------------------------------


class TestNormalizeOpportunityType:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("internship", "internship"),
            ("Internship", "internship"),
            ("intern", "internship"),
            ("summer intern", "internship"),
            ("fte", "full_time"),
            ("full time", "full_time"),
            ("full-time", "full_time"),
            ("fulltime", "full_time"),
            ("permanent", "full_time"),
            ("contract", "contract"),
            ("part time", "part_time"),
            ("part-time", "part_time"),
        ],
    )
    def test_synonyms(self, raw, expected):
        assert normalize_opportunity_type(raw) == expected

    def test_unknown_returns_normalised_raw(self):
        assert normalize_opportunity_type("apprenticeship") == "apprenticeship"

    def test_none(self):
        assert normalize_opportunity_type(None) == ""


# ---------------------------------------------------------------------------
# exact_match
# ---------------------------------------------------------------------------


class TestExactMatch:
    def test_equal_strings(self):
        assert exact_match("google", "google") is True

    def test_different_strings(self):
        assert exact_match("google", "microsoft") is False

    def test_empty_strings(self):
        assert exact_match("", "") is True


# ---------------------------------------------------------------------------
# exact_match_fields
# ---------------------------------------------------------------------------


class TestExactMatchFields:
    def test_all_exact(self):
        inc = _opp("Google", "SWE Intern", "internship")
        cnd = _opp("Google", "SWE Intern", "internship")
        c, r, t = exact_match_fields(inc, cnd)
        assert c and r and t

    def test_company_mismatch(self):
        inc = _opp("Google", "SWE Intern", "internship")
        cnd = _opp("Microsoft", "SWE Intern", "internship")
        c, r, t = exact_match_fields(inc, cnd)
        assert not c
        assert r
        assert t

    def test_type_synonym_treated_as_exact(self):
        # "intern" and "internship" both map to "internship"
        inc = _opp("Google", "SWE Intern", "intern")
        cnd = _opp("Google", "SWE Intern", "internship")
        _, _, t = exact_match_fields(inc, cnd)
        assert t


# ---------------------------------------------------------------------------
# Fuzzy scoring
# ---------------------------------------------------------------------------


class TestFuzzyScoring:
    def test_identical_companies_score_100(self):
        assert fuzzy_score_company("Google", "Google") == 100.0

    def test_google_vs_google_llc_high_score(self):
        score = fuzzy_score_company("Google", "Google LLC")
        assert score >= 85.0, score

    def test_very_different_companies_low_score(self):
        score = fuzzy_score_company("Google", "Tata Motors")
        assert score < 60.0, score

    def test_identical_roles_score_100(self):
        assert fuzzy_score_role("Software Engineer", "Software Engineer") == 100.0

    def test_role_word_order_independence(self):
        score = fuzzy_score_role("Software Engineer Intern", "Intern Software Engineer")
        assert score >= 90.0, score

    def test_exact_type_map_scores_100(self):
        assert fuzzy_score_type("intern", "internship") == 100.0

    def test_internship_vs_fulltime_low_score(self):
        score = fuzzy_score_type("internship", "full time")
        assert score < 60.0, score


# ---------------------------------------------------------------------------
# FieldScore
# ---------------------------------------------------------------------------


class TestFieldScore:
    def test_effective_score_exact(self):
        fs = FieldScore("company_name", "google", "google", exact_match=True)
        assert fs.effective_score == 100.0

    def test_effective_score_fuzzy(self):
        fs = FieldScore("company_name", "google", "googlee", exact_match=False, fuzzy_score=92.0)
        assert fs.effective_score == 92.0

    def test_effective_score_no_fuzzy(self):
        fs = FieldScore("company_name", "google", "amazon", exact_match=False, fuzzy_score=None)
        assert fs.effective_score == 0.0


# ---------------------------------------------------------------------------
# score_* builders
# ---------------------------------------------------------------------------


class TestScoreBuilders:
    def test_score_company_exact(self):
        inc = _opp("Google")
        cnd = _opp("Google")
        fs = score_company(inc, cnd)
        assert fs.field_name == FIELD_COMPANY
        assert fs.exact_match is True
        assert fs.effective_score == 100.0

    def test_score_role_fuzzy(self):
        inc = _opp(role="Software Engineer Intern")
        cnd = _opp(role="SWE Intern")
        fs = score_role(inc, cnd)
        assert fs.fuzzy_score is not None

    def test_score_type_synonym(self):
        inc = _opp(opp_type="intern")
        cnd = _opp(opp_type="Internship")
        fs = score_type(inc, cnd)
        assert fs.exact_match is True


# ---------------------------------------------------------------------------
# compute_confidence_score
# ---------------------------------------------------------------------------


class TestComputeConfidenceScore:
    def test_all_100_gives_100(self, default_config):
        c = FieldScore(FIELD_COMPANY, "a", "a", exact_match=True)
        r = FieldScore(FIELD_ROLE, "b", "b", exact_match=True)
        t = FieldScore(FIELD_TYPE, "c", "c", exact_match=True)
        score = compute_confidence_score(c, r, t, default_config)
        assert score == 100.0

    def test_all_0_gives_0(self, default_config):
        c = FieldScore(FIELD_COMPANY, "a", "x", exact_match=False, fuzzy_score=0.0)
        r = FieldScore(FIELD_ROLE, "b", "y", exact_match=False, fuzzy_score=0.0)
        t = FieldScore(FIELD_TYPE, "c", "z", exact_match=False, fuzzy_score=0.0)
        score = compute_confidence_score(c, r, t, default_config)
        assert score == 0.0

    def test_weighted_combination(self):
        config = DeduplicationConfig(
            company_weight=0.5,
            role_weight=0.3,
            type_weight=0.2,
        )
        c = FieldScore(FIELD_COMPANY, "", "", exact_match=False, fuzzy_score=80.0)
        r = FieldScore(FIELD_ROLE, "", "", exact_match=False, fuzzy_score=60.0)
        t = FieldScore(FIELD_TYPE, "", "", exact_match=False, fuzzy_score=100.0)
        score = compute_confidence_score(c, r, t, config)
        expected = round(80 * 0.5 + 60 * 0.3 + 100 * 0.2, 2)
        assert score == expected


# ---------------------------------------------------------------------------
# DeduplicationConfig validation
# ---------------------------------------------------------------------------


class TestDeduplicationConfig:
    def test_default_weights_sum_to_one(self):
        cfg = DeduplicationConfig()
        total = cfg.company_weight + cfg.role_weight + cfg.type_weight
        assert abs(total - 1.0) < 1e-6

    def test_invalid_weights_raise(self):
        with pytest.raises(ValueError, match="sum to 1.0"):
            DeduplicationConfig(company_weight=0.5, role_weight=0.5, type_weight=0.5)


# ---------------------------------------------------------------------------
# compare_opportunities
# ---------------------------------------------------------------------------


class TestCompareOpportunities:
    def test_identical_records_are_exact_duplicates(self, default_config):
        inc = _opp("Google", "SWE Intern", "internship")
        cnd = _opp("Google", "SWE Intern", "internship")
        result = compare_opportunities(inc, cnd, config=default_config)
        assert result.is_duplicate is True
        assert result.is_exact is True
        assert result.confidence_score == 100.0

    def test_clearly_different_records_not_duplicate(self, default_config):
        inc = _opp("Google", "Data Engineer", "full time")
        cnd = _opp("Amazon", "ML Researcher", "internship")
        result = compare_opportunities(inc, cnd, config=default_config)
        assert result.is_duplicate is False

    def test_legal_suffix_difference_still_duplicate(self, default_config):
        inc = _opp("Infosys Technologies Pvt. Ltd.", "Software Engineer", "full time")
        cnd = _opp("Infosys", "Software Engineer", "full_time")
        result = compare_opportunities(inc, cnd, config=default_config)
        assert result.is_duplicate is True

    def test_type_mismatch_with_require_type_match(self):
        config = DeduplicationConfig(require_type_match=True)
        inc = _opp("Google", "SWE", "internship")
        cnd = _opp("Google", "SWE", "full time")
        result = compare_opportunities(inc, cnd, config=config)
        assert result.is_duplicate is False

    def test_type_mismatch_without_require_type_match(self):
        config = DeduplicationConfig(require_type_match=False)
        inc = _opp("Google", "SWE", "internship")
        cnd = _opp("Google", "SWE", "full time")
        result = compare_opportunities(inc, cnd, config=config)
        # Company and role are identical → should still be flagged
        assert result.is_duplicate is True

    def test_candidate_id_property(self, default_config):
        inc = _opp()
        cnd = _opp(id=42)
        result = compare_opportunities(inc, cnd, config=default_config)
        assert result.candidate_id == 42

    def test_summary_contains_duplicate_keyword(self, default_config):
        inc = _opp()
        cnd = _opp()
        result = compare_opportunities(inc, cnd, config=default_config)
        assert "DUPLICATE" in result.summary()

    def test_summary_contains_unique_keyword(self, default_config):
        inc = _opp("Google", "SWE", "internship")
        cnd = _opp("Amazon", "ML Engineer", "full time")
        result = compare_opportunities(inc, cnd, config=default_config)
        assert "UNIQUE" in result.summary()


# ---------------------------------------------------------------------------
# find_all_matches / find_best_match / is_duplicate
# ---------------------------------------------------------------------------


class TestBatchHelpers:
    def _make_candidates(self) -> list[dict]:
        return [
            _opp("Google", "SWE Intern", "internship", id=1),
            _opp("Amazon", "ML Engineer", "full time", id=2),
            _opp("Microsoft", "PM Intern", "internship", id=3),
        ]

    def test_find_all_matches_returns_only_duplicates(self, default_config):
        inc = _opp("Google", "SWE Intern", "internship")
        candidates = self._make_candidates()
        matches = find_all_matches(inc, candidates, config=default_config)
        assert all(m.is_duplicate for m in matches)
        assert len(matches) >= 1
        assert matches[0].candidate_id == 1  # highest confidence

    def test_find_all_matches_sorted_by_descending_confidence(self, default_config):
        inc = _opp("Google", "SWE Intern", "internship")
        candidates = self._make_candidates()
        matches = find_all_matches(inc, candidates, config=default_config)
        scores = [m.confidence_score for m in matches]
        assert scores == sorted(scores, reverse=True)

    def test_find_best_match_returns_top_hit(self, default_config):
        inc = _opp("Google", "SWE Intern", "internship")
        candidates = self._make_candidates()
        best = find_best_match(inc, candidates, config=default_config)
        assert best is not None
        assert best.candidate_id == 1

    def test_find_best_match_returns_none_when_no_duplicate(self, default_config):
        inc = _opp("Tata Motors", "Production Engineer", "full time")
        candidates = self._make_candidates()
        assert find_best_match(inc, candidates, config=default_config) is None

    def test_is_duplicate_true(self, default_config):
        inc = _opp("Google", "SWE Intern", "internship")
        candidates = self._make_candidates()
        assert is_duplicate(inc, candidates, config=default_config) is True

    def test_is_duplicate_false(self, default_config):
        inc = _opp("Tata Motors", "Plant Manager", "full time")
        candidates = self._make_candidates()
        assert is_duplicate(inc, candidates, config=default_config) is False

    def test_empty_candidates_list(self, default_config):
        inc = _opp()
        assert find_best_match(inc, [], config=default_config) is None
        assert is_duplicate(inc, [], config=default_config) is False


# ---------------------------------------------------------------------------
# detect_updates
# ---------------------------------------------------------------------------


class TestDetectUpdates:
    def test_no_changes_returns_empty_list(self, default_config):
        inc = _opp(deadline="2026-07-01", package_or_stipend="80k")
        cnd = _opp(deadline="2026-07-01", package_or_stipend="80k")
        changes = detect_updates(inc, cnd)
        assert changes == []

    def test_changed_deadline_detected(self):
        inc = _opp(deadline="2026-08-01")
        cnd = _opp(deadline="2026-07-01")
        changes = detect_updates(inc, cnd)
        field_names = [c.field_name for c in changes]
        assert "deadline" in field_names

    def test_new_field_value_detected(self):
        inc = _opp(package_or_stipend="90k")
        cnd = _opp(package_or_stipend=None)
        changes = detect_updates(inc, cnd)
        assert any(c.field_name == "package_or_stipend" for c in changes)

    def test_updated_field_carries_old_and_new_values(self):
        inc = _opp(deadline="2026-09-01")
        cnd = _opp(deadline="2026-07-01")
        changes = detect_updates(inc, cnd)
        d = next(c for c in changes if c.field_name == "deadline")
        assert d.old_value == "2026-07-01"
        assert d.new_value == "2026-09-01"

    def test_none_and_empty_string_treated_as_equal(self):
        inc = _opp(deadline=None)
        cnd = _opp(deadline="")
        changes = detect_updates(inc, cnd)
        deadline_changes = [c for c in changes if c.field_name == "deadline"]
        assert deadline_changes == []


# ---------------------------------------------------------------------------
# find_duplicates_in_list
# ---------------------------------------------------------------------------


class TestFindDuplicatesInList:
    def test_identical_pair_found(self, default_config):
        opps = [
            _opp("Google", "SWE Intern", "internship", id=1),
            _opp("Google", "SWE Intern", "internship", id=2),
            _opp("Amazon", "ML Engineer", "full time", id=3),
        ]
        pairs = find_duplicates_in_list(opps, config=default_config)
        assert len(pairs) >= 1
        i, j, result = pairs[0]
        assert i < j
        assert result.is_duplicate is True

    def test_no_duplicates_returns_empty(self, default_config):
        opps = [
            _opp("Google", "SWE", "internship", id=1),
            _opp("Amazon", "ML Engineer", "full time", id=2),
            _opp("Microsoft", "PM", "internship", id=3),
        ]
        pairs = find_duplicates_in_list(opps, config=default_config)
        assert pairs == []

    def test_single_opportunity_returns_empty(self, default_config):
        assert find_duplicates_in_list([_opp()], config=default_config) == []

    def test_empty_list_returns_empty(self, default_config):
        assert find_duplicates_in_list([], config=default_config) == []


# ---------------------------------------------------------------------------
# rapidfuzz_prefilter
# ---------------------------------------------------------------------------


class TestRapidfuzzPrefilter:
    def test_returns_relevant_candidates(self):
        candidates = [
            _opp("Google", id=1),
            _opp("Amazon", id=2),
            _opp("Microsoft", id=3),
        ]
        result = rapidfuzz_prefilter("Google", candidates, limit=5, score_cutoff=80.0)
        ids = [c["id"] for c in result]
        assert 1 in ids  # Google should be shortlisted

    def test_empty_candidates_returns_empty(self):
        assert rapidfuzz_prefilter("Google", [], limit=5) == []

    def test_respects_limit(self):
        candidates = [_opp(f"Company {i}", id=i) for i in range(20)]
        result = rapidfuzz_prefilter("Company", candidates, limit=5, score_cutoff=0.0)
        assert len(result) <= 5

    def test_none_company_handled(self):
        candidates = [_opp("Google", id=1)]
        # Should not raise
        result = rapidfuzz_prefilter(None, candidates)
        assert isinstance(result, list)
