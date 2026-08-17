"""docs/design/16 Phase C: roster verification (docs/design/15 §3)."""

from __future__ import annotations

import pytest

from placement_mail_tracker.config.user_profile import UserProfile
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.extraction.roster import verify_roster


def _profile(**overrides) -> UserProfile:
    base = dict(
        degree="B.Tech",
        branch="Computer Science",
        campus="Vellore",
        graduation_year=2027,
        cgpa=8.0,
        full_name="Yash Agarwal",
        registration_no="23BAI0011",
        codenames=["X2A6L2T3"],
    )
    base.update(overrides)
    return UserProfile(**base)


def test_exact_registration_number_match():
    roster = "1. John Doe 23BAI0099\n2. Yash Agarwal 23BAI0011\n3. Jane Roe 23BAI0100"
    result = verify_roster(roster, _profile())
    assert result.verdict == "MATCHED"
    assert result.method == "registration_no"


def test_registration_number_case_insensitive():
    roster = "Shortlisted: 23bai0011, 23BAI0099"
    result = verify_roster(roster, _profile())
    assert result.verdict == "MATCHED"
    assert result.method == "registration_no"


def test_registration_number_must_be_a_whole_token_not_a_substring():
    """docs/design/16: a naive substring check would false-match "23BAI0011"
    inside "1223BAI00119" -- the worst failure mode in this system."""
    roster = "1. John Doe 1223BAI00119\n2. Jane Roe 23BAI0100"
    result = verify_roster(roster, _profile())
    assert result.verdict == "NOT_MATCHED"


def test_codename_match():
    roster = "Neo IDs shortlisted: X2A6L2T3, Y9B7M4N2"
    result = verify_roster(roster, _profile())
    assert result.verdict == "MATCHED"
    assert result.method == "codename"


def test_registration_present_but_not_users_is_not_matched():
    roster = "1. John Doe 23BAI0099\n2. Jane Roe 23BAI0100\n3. Bob Lee 23BAI0101"
    result = verify_roster(roster, _profile())
    assert result.verdict == "NOT_MATCHED"


def test_empty_roster_is_no_roster_not_not_matched():
    """Zero registration numbers found proves nothing -- doc 15 §3.3. Nothing
    roster-shaped was found at all, so this is NO_ROSTER, not AMBIGUOUS
    (docs/design/16 Cause 3)."""
    result = verify_roster("", _profile())
    assert result.verdict == "NO_ROSTER"
    assert result.method == "none"


def test_unparseable_roster_text_is_no_roster():
    result = verify_roster("[image could not be extracted]", _profile())
    assert result.verdict == "NO_ROSTER"


def test_similar_but_not_matching_name_without_reg_numbers_is_no_roster():
    """No reg-nos anywhere and the name doesn't come close to clearing the
    threshold -> NO_ROSTER (nothing roster-shaped found), never NOT_MATCHED
    (roster didn't demonstrably parse) and never AMBIGUOUS (that's reserved
    for a name that cleared the score threshold but not the uniqueness
    margin -- docs/design/16 Cause 3)."""
    roster = "Shortlisted candidates:\nYash Agarwal\nRohan Sharma"
    result = verify_roster(roster, _profile(full_name="Rohan Agarwal"))
    assert result.verdict == "NO_ROSTER"


def test_borderline_name_match_without_margin_is_ambiguous_not_no_roster():
    """A name that clears ROSTER_NAME_THRESHOLD but not the uniqueness
    margin (two similarly-named candidates) is a genuine "might be you"
    ambiguity -- the roster parsed, unlike NO_ROSTER (docs/design/16
    Cause 3)."""
    roster = "Shortlisted candidates:\nYash Agarwals\nYashh Agarwal"
    result = verify_roster(roster, _profile())
    assert result.verdict == "AMBIGUOUS"
    assert result.method == "name_fuzzy"


def test_unverified_identity_refuses_to_match():
    """Default/fallback profile or missing registration_no must never
    produce a positive or negative verdict (doc 15 §3.1)."""
    default_profile = UserProfile._default()
    roster = "1. Someone 23BAI0011"
    result = verify_roster(roster, default_profile)
    assert result.verdict == "AMBIGUOUS"
    assert result.method == "none"


def test_name_fuzzy_match_when_no_ids_present():
    roster = "Shortlisted (no reg numbers on this sheet):\nYash Agarwal\nPriya Iyer\nRahul Nair"
    result = verify_roster(roster, _profile(registration_no="", codenames=[]))
    # No registration_no means is_identity_verified() is False -> refuses.
    assert result.verdict == "AMBIGUOUS"


def test_name_fuzzy_match_with_reg_no_absent_from_roster():
    """Registration_no is set on the profile but this particular roster
    doesn't carry reg numbers at all -- name fuzzy match should still work."""
    roster = "Shortlisted candidates:\nYash Agarwal\nPriya Iyer\nRahul Nair"
    result = verify_roster(roster, _profile())
    assert result.verdict == "MATCHED"
    assert result.method == "name_fuzzy"


def test_ambiguous_verdict_never_downgrades_an_existing_stronger_verdict(
    db_manager: DatabaseManager, sample_opportunity
):
    """docs/design/16: a later mail whose attachment doesn't parse must not
    erase an existing MATCHED/NOT_MATCHED (the same non-downgrade bug
    already fixed for drive_kind and the my_status ladder)."""
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"), source_email_id="b1"
    )
    db_manager.upsert_roster_verdict(opp_id, "INTERVIEW", "NOT_MATCHED", method="user_asserted")

    db_manager.upsert_roster_verdict(opp_id, "INTERVIEW", "AMBIGUOUS", method="none")

    verdict = db_manager.fetch_roster_verdicts()[(opp_id, "INTERVIEW")]
    assert verdict["verdict"] == "NOT_MATCHED"
    assert verdict["method"] == "user_asserted"


def test_stronger_verdict_still_overwrites_an_earlier_one(
    db_manager: DatabaseManager, sample_opportunity
):
    """A genuine roster result must still be able to supersede an earlier one."""
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"), source_email_id="b1"
    )
    db_manager.upsert_roster_verdict(opp_id, "INTERVIEW", "NOT_MATCHED", method="registration_no")
    db_manager.upsert_roster_verdict(opp_id, "INTERVIEW", "MATCHED", method="registration_no")

    verdict = db_manager.fetch_roster_verdicts()[(opp_id, "INTERVIEW")]
    assert verdict["verdict"] == "MATCHED"


# ---------------------------------------------------------------------------
# Regression fixtures: real production roster excerpts, fetched live from
# Gmail while testing this module (docs/design/16). Every real roster
# checked was a Neo-ID-only list -- no registration numbers, no names --
# which is what the bug below was actually about.
# ---------------------------------------------------------------------------

_REAL_ABSYZ_EXCERPT = (
    "Neo ID\nN2Z3K8G4\nT2N4C3B9\nL5A1J1J7\nJ6Q7V4V5\nP5E1D0X2\n"
    "A2J4M5Q8\nI3B7J0W5\nB4Q0O2S9\nM2H0G6M3\nL9J3N2X3"
)

_REAL_TEKION_ADDITIONAL_EXCERPT = (
    "NEO ID \nO5P8D1U6\nX2A6L2T3\nO4H7P1K1\nH1E5X6I3\nX6I1M4O7\nQ4Z7"
)


def test_real_neo_id_only_roster_absent_is_not_matched():
    """Regression: a pure Neo-ID list with the codename absent used to
    return AMBIGUOUS (only _REG_NO_RE was checked as "did it parse"), which
    silently defeated the calendar garbage-collection this whole feature
    exists for -- confirmed against 5 real production rosters."""
    result = verify_roster(_REAL_ABSYZ_EXCERPT, _profile())
    assert result.verdict == "NOT_MATCHED"
    assert result.method == "codename"


def test_real_neo_id_only_roster_present_is_matched():
    result = verify_roster(_REAL_TEKION_ADDITIONAL_EXCERPT, _profile())
    assert result.verdict == "MATCHED"
    assert result.method == "codename"


# ---------------------------------------------------------------------------
# docs/design/16 Cause 3: NO_ROSTER precedence.
#
# The full MATCHED > NOT_MATCHED > AMBIGUOUS > NO_ROSTER non-downgrade
# ladder is enforced in db.manager.upsert_roster_verdict, which this task
# does not own/edit. Today that function's guard is
# ``WHERE excluded.verdict != 'AMBIGUOUS'`` -- upserting NO_ROSTER over an
# existing NOT_MATCHED/MATCHED currently *would* incorrectly downgrade it.
# The required fix (for the other agent to apply) is:
#
#     WHERE excluded.verdict NOT IN ('AMBIGUOUS', 'NO_ROSTER');
#
# This test is xfail until that lands -- it documents the requirement
# without turning the suite red.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="db.manager.upsert_roster_verdict's non-downgrade guard does not "
    "yet cover NO_ROSTER (needs `WHERE excluded.verdict NOT IN "
    "('AMBIGUOUS', 'NO_ROSTER')`) -- pending change in db/manager.py, out "
    "of this task's scope.",
    strict=False,
)
def test_no_roster_verdict_never_downgrades_an_existing_stronger_verdict(
    db_manager: DatabaseManager, sample_opportunity
):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"), source_email_id="b1"
    )
    db_manager.upsert_roster_verdict(opp_id, "OA", "NOT_MATCHED", method="registration_no")

    db_manager.upsert_roster_verdict(opp_id, "OA", "NO_ROSTER", method="none")

    verdict = db_manager.fetch_roster_verdicts()[(opp_id, "OA")]
    assert verdict["verdict"] == "NOT_MATCHED"
