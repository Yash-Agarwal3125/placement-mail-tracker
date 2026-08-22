"""Safety-nets plan (2026-08-18), Phase 3: make fragmentation loud.

Detection only -- never merges. Group active opportunities sharing
normalised company + year; skip the known-legitimate concurrent-drive
counts (Honeywell x3, Flipkart x2) so the signal stays meaningful.
"""

from __future__ import annotations

from placement_mail_tracker.utils.duplicate_drive_detection import (
    find_duplicate_drive_groups,
    format_duplicate_drive_warnings,
)


def _seed_active(db_manager, company, role, current_status="OPEN"):
    db_manager.insert_or_update_opportunity(
        {
            "company_name": company,
            "role": role,
            "current_status": current_status,
            "eligibility_status": "ELIGIBLE",
        },
        source_email_id=f"seed-{company}-{role}",
    )


def _opp(id_, company, year="2026", **overrides):
    base = {
        "id": id_,
        "company_name": company,
        "created_at": f"{year}-06-01T00:00:00",
        "email_received_at": f"{year}-06-01T00:00:00",
    }
    base.update(overrides)
    return base


class TestFindDuplicateDriveGroups:
    def test_two_same_company_year_drives_flagged(self):
        opps = [_opp(1, "Blackrock"), _opp(2, "Blackrock")]
        groups = find_duplicate_drive_groups(opps)
        assert len(groups) == 1
        (key, rows), = groups.items()
        assert len(rows) == 2

    def test_single_drive_per_company_year_not_flagged(self):
        opps = [_opp(1, "Blackrock"), _opp(2, "Foodhub")]
        assert find_duplicate_drive_groups(opps) == {}

    def test_different_years_not_flagged(self):
        opps = [_opp(1, "Blackrock", year="2025"), _opp(2, "Blackrock", year="2026")]
        assert find_duplicate_drive_groups(opps) == {}

    def test_honeywell_at_known_count_of_three_is_silent(self):
        opps = [_opp(i, "Honeywell") for i in (1, 2, 3)]
        assert find_duplicate_drive_groups(opps) == {}

    def test_honeywell_beyond_known_count_still_warns(self):
        """The allowlist is count-based, not a blanket suppression -- a
        4th concurrent Honeywell drive must not stay silent forever."""
        opps = [_opp(i, "Honeywell") for i in (1, 2, 3, 4)]
        groups = find_duplicate_drive_groups(opps)
        assert len(groups) == 1
        (key, rows), = groups.items()
        assert len(rows) == 4

    def test_flipkart_at_known_count_of_two_is_silent(self):
        opps = [_opp(1, "Flipkart"), _opp(2, "Flipkart")]
        assert find_duplicate_drive_groups(opps) == {}

    def test_flipkart_beyond_known_count_warns(self):
        opps = [_opp(i, "Flipkart") for i in (1, 2, 3)]
        assert len(find_duplicate_drive_groups(opps)) == 1

    def test_unknown_company_rows_never_grouped(self):
        """Junk/placeholder company names must never form a false-positive
        duplicate group -- there are ten-plus 'Unknown Company' rows in
        production."""
        opps = [
            _opp(1, "Unknown Company"),
            _opp(2, "Unknown Company"),
            _opp(3, "Unknown"),
            _opp(4, None),
        ]
        assert find_duplicate_drive_groups(opps) == {}


class TestFormatDuplicateDriveWarnings:
    def test_line_names_ids_and_company(self):
        opps = [_opp(1, "Blackrock"), _opp(2, "Blackrock")]
        groups = find_duplicate_drive_groups(opps)
        lines = format_duplicate_drive_warnings(groups)
        assert len(lines) == 1
        assert "Blackrock" in lines[0]
        assert "1" in lines[0] and "2" in lines[0]


class TestRunnerWiring:
    def test_detect_duplicate_drives_logs_a_warning(
        self, db_manager, mock_settings, caplog
    ):
        """End-to-end through PlacementTrackerRunner._detect_duplicate_drives:
        two fragmented Blackrock rows produce a logged warning (safety-nets
        plan Phase 3 acceptance)."""
        from placement_mail_tracker.scheduler.runner import PlacementTrackerRunner

        _seed_active(db_manager, "Blackrock", "Software Engineer")
        _seed_active(db_manager, "Blackrock", "Assistant Manager")

        runner = PlacementTrackerRunner(
            connection=db_manager.connection, settings=mock_settings
        )
        with caplog.at_level("WARNING"):
            runner._detect_duplicate_drives(db_manager)

        warnings = [r.message for r in caplog.records if "DUPLICATE DRIVE" in r.message]
        assert len(warnings) == 1
        assert "Blackrock" in warnings[0]

    def test_detect_duplicate_drives_silent_for_known_allowlisted_pair(
        self, db_manager, mock_settings, caplog
    ):
        from placement_mail_tracker.scheduler.runner import PlacementTrackerRunner

        _seed_active(db_manager, "Flipkart", "Super Dream PPO")
        _seed_active(db_manager, "Flipkart", "Super Dream Internship")

        runner = PlacementTrackerRunner(
            connection=db_manager.connection, settings=mock_settings
        )
        with caplog.at_level("WARNING"):
            runner._detect_duplicate_drives(db_manager)

        warnings = [r.message for r in caplog.records if "DUPLICATE DRIVE" in r.message]
        assert warnings == []
