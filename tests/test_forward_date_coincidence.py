from datetime import datetime, timedelta

from placement_mail_tracker.utils.deduplication import has_event_coincidence


def _iso(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%dT10:00")


def test_future_round_after_older_date_counts_as_same_drive():
    assert has_event_coincidence({"oa_date": _iso(3)}, {"oa_date": _iso(-60)})


def test_earlier_unrelated_date_still_not_coincident():
    assert not has_event_coincidence({"oa_date": _iso(3)}, {"oa_date": _iso(30)})
