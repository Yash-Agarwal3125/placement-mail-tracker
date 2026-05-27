"""Tests for SQLite database manager."""

from placement_mail_tracker.db.connection import get_connection
from placement_mail_tracker.db.manager import DatabaseManager, generate_unique_hash


def make_manager(tmp_path) -> DatabaseManager:
    connection = get_connection(tmp_path / "test.db")
    manager = DatabaseManager(connection=connection)
    manager.create_tables()
    return manager


def test_insert_opportunity_creates_record_and_event(tmp_path) -> None:
    manager = make_manager(tmp_path)

    opportunity_id, created = manager.insert_or_update_opportunity(
        {
            "company_name": "ExampleTech",
            "role": "SDE Intern",
            "internship_or_fulltime": "internship",
            "branches_allowed": ["CSE", "IT"],
            "hiring_process": ["OA", "Interview"],
        },
        source_email_id="msg-1",
    )

    active = manager.get_active_opportunities()
    events = manager.get_opportunity_events(opportunity_id)

    assert created is True
    assert active[0]["company_name"] == "ExampleTech"
    assert active[0]["unique_hash"] == generate_unique_hash("ExampleTech", "SDE Intern")
    assert active[0]["branches_allowed"] == ["CSE", "IT"]
    assert events[0]["update_type"] == "created"


def test_duplicate_company_role_updates_existing_record(tmp_path) -> None:
    manager = make_manager(tmp_path)
    first_id, created = manager.insert_or_update_opportunity(
        {
            "company_name": "ExampleTech",
            "role": "SDE Intern",
            "deadline": "2026-05-30",
        }
    )

    second_id, second_created = manager.insert_or_update_opportunity(
        {
            "company_name": "exampletech",
            "role": "sde intern",
            "deadline": "2026-06-01",
            "package_or_stipend": "50000 per month",
        }
    )

    active = manager.get_active_opportunities()
    events = manager.get_opportunity_events(first_id)

    assert created is True
    assert second_created is False
    assert second_id == first_id
    assert len(active) == 1
    assert active[0]["deadline"] == "2026-06-01"
    assert any(event["field_name"] == "deadline" for event in events)
    assert any(event["field_name"] == "package_or_stipend" for event in events)


def test_exact_duplicate_adds_duplicate_seen_event(tmp_path) -> None:
    manager = make_manager(tmp_path)
    opportunity = {"company_name": "ExampleTech", "role": "Backend Engineer"}

    opportunity_id, _ = manager.insert_or_update_opportunity(opportunity)
    _, created = manager.insert_or_update_opportunity(opportunity)

    events = manager.get_opportunity_events(opportunity_id)

    assert created is False
    assert events[-1]["update_type"] == "duplicate_seen"


def test_email_log_is_upserted_by_gmail_message_id(tmp_path) -> None:
    manager = make_manager(tmp_path)

    first_log_id = manager.log_email(
        gmail_message_id="gmail-1",
        subject="Campus drive",
        sender="CDC <cdc@example.edu>",
        filter_score=88,
        filter_decision={"is_placement": True},
        processed_status="processed",
    )
    second_log_id = manager.log_email(
        gmail_message_id="gmail-1",
        subject="Campus drive updated",
        processed_status="processed",
    )

    row = manager.connection.execute(
        "SELECT subject FROM processed_emails WHERE gmail_message_id = ?",
        ("gmail-1",),
    ).fetchone()

    assert second_log_id == first_log_id
    assert row["subject"] == "Campus drive updated"


def test_update_opportunity_creates_update_events(tmp_path) -> None:
    manager = make_manager(tmp_path)
    opportunity_id, _ = manager.insert_or_update_opportunity(
        {"company_name": "ExampleTech", "role": "SDE Intern"}
    )

    manager.update_opportunity(
        opportunity_id,
        {
            "company_name": "ExampleTech",
            "role": "SDE Intern",
            "work_location": "Bengaluru",
        },
    )

    opportunity = manager.fetch_opportunity_by_id(opportunity_id)
    updates = manager.fetch_updates_for_opportunity(opportunity_id)

    assert opportunity is not None
    assert opportunity["work_location"] == "Bengaluru"
    assert any(update["field_name"] == "work_location" for update in updates)


def test_create_update_event_can_store_manual_notes(tmp_path) -> None:
    manager = make_manager(tmp_path)
    opportunity_id, _ = manager.insert_or_update_opportunity(
        {"company_name": "ExampleTech", "role": "SDE Intern"}
    )

    update_id = manager.create_update_event(
        opportunity_id,
        "manual_note",
        notes="Deadline verified manually",
    )

    updates = manager.fetch_updates_for_opportunity(opportunity_id)

    assert update_id > 0
    assert updates[-1]["update_type"] == "manual_note"
    assert updates[-1]["notes"] == "Deadline verified manually"


def test_notifications_can_be_created_and_fetched(tmp_path) -> None:
    manager = make_manager(tmp_path)

    notification_id = manager.create_notification(
        channel="email",
        recipient="student@example.com",
        subject="New placement update",
        message="ExampleTech opened applications.",
        status="sent",
        sent_at="2026-05-27T10:00:00+00:00",
    )

    notifications = manager.fetch_notifications(status="sent")

    assert notification_id > 0
    assert notifications[0]["channel"] == "email"
    assert notifications[0]["status"] == "sent"


def test_active_opportunities_excludes_closed_records(tmp_path) -> None:
    manager = make_manager(tmp_path)
    opportunity_id, _ = manager.insert_or_update_opportunity(
        {"company_name": "ExampleTech", "role": "SDE Intern"}
    )
    manager.connection.execute(
        "UPDATE opportunities SET status = 'closed' WHERE id = ?",
        (opportunity_id,),
    )
    manager.connection.commit()

    assert manager.get_active_opportunities() == []
