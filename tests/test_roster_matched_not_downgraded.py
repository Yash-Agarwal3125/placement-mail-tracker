def test_later_not_matched_does_not_erase_matched(db_manager):
    db_manager.upsert_roster_verdict(1, "INTERVIEW", "MATCHED", method="codename")
    db_manager.upsert_roster_verdict(1, "INTERVIEW", "NOT_MATCHED", method="codename")
    assert db_manager.fetch_roster_verdicts()[(1, "INTERVIEW")]["verdict"] == "MATCHED"


def test_not_matched_can_still_become_matched(db_manager):
    db_manager.upsert_roster_verdict(2, "OA", "NOT_MATCHED", method="codename")
    db_manager.upsert_roster_verdict(2, "OA", "MATCHED", method="codename")
    assert db_manager.fetch_roster_verdicts()[(2, "OA")]["verdict"] == "MATCHED"
