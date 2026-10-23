from placement_mail_tracker.utils.deduplication import has_event_coincidence


def test_future_round_after_older_date_counts_as_same_drive():
    candidate = {"oa_date": "2020-08-06T14:00"}
    assert has_event_coincidence({"oa_date": "2999-10-26T10:00"}, candidate)


def test_earlier_unrelated_date_still_not_coincident():
    candidate = {"oa_date": "2999-10-26T10:00"}
    assert not has_event_coincidence({"oa_date": "2999-01-01T10:00"}, candidate)
