"""docs/design/16 Phase C: roster verification (docs/design/15 §3)."""

from __future__ import annotations

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


def test_empty_roster_is_ambiguous_not_not_matched():
    """Zero registration numbers found proves nothing -- doc 15 §3.3."""
    result = verify_roster("", _profile())
    assert result.verdict == "AMBIGUOUS"


def test_unparseable_roster_text_is_ambiguous():
    result = verify_roster("[image could not be extracted]", _profile())
    assert result.verdict == "AMBIGUOUS"


def test_similar_but_not_matching_name_without_reg_numbers_is_ambiguous():
    """No reg-nos anywhere and the name doesn't clear the margin -> AMBIGUOUS,
    never NOT_MATCHED (roster didn't demonstrably parse)."""
    roster = "Shortlisted candidates:\nYash Agarwal\nRohan Sharma"
    result = verify_roster(roster, _profile(full_name="Rohan Agarwal"))
    assert result.verdict == "AMBIGUOUS"


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
