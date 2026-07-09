from pathlib import Path

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.config.validator import ConfigValidator


def test_validate_file_exists(tmp_path: Path):
    settings = Settings()
    validator = ConfigValidator(settings)
    
    test_file = tmp_path / "test.json"
    test_file.write_text("{}")
    
    validator.validate_file(test_file, "Test File", is_critical=True)
    assert len(validator.results) == 1
    assert validator.results[0].status == "PASS"
    assert validator.is_healthy() is True


def test_validate_file_missing_critical(tmp_path: Path):
    settings = Settings()
    validator = ConfigValidator(settings)
    
    test_file = tmp_path / "missing.json"
    
    validator.validate_file(test_file, "Missing File", is_critical=True)
    assert len(validator.results) == 1
    assert validator.results[0].status == "ERROR"
    assert validator.is_healthy() is False


def test_validate_file_missing_optional(tmp_path: Path):
    settings = Settings()
    validator = ConfigValidator(settings)
    
    test_file = tmp_path / "optional.json"
    
    validator.validate_file(test_file, "Optional File", is_critical=False)
    assert len(validator.results) == 1
    assert validator.results[0].status == "WARNING"
    assert validator.is_healthy() is True  # WARNING shouldn't fail health


def test_validate_env_var():
    settings = Settings()
    validator = ConfigValidator(settings)
    
    # Passing var
    validator.validate_env_var("actual_key", "Test Key", is_critical=True)
    assert validator.results[0].status == "PASS"
    
    # Missing critical var
    validator.validate_env_var("", "Missing Key", is_critical=True)
    assert validator.results[1].status == "ERROR"
    
    # Missing optional var
    validator.validate_env_var("", "Optional Key", is_critical=False)
    assert validator.results[2].status == "WARNING"
    
    assert validator.is_healthy() is False


def test_validate_directory_auto_creation(tmp_path: Path):
    settings = Settings()
    validator = ConfigValidator(settings)
    
    target_dir = tmp_path / "new_folder"
    assert not target_dir.exists()
    
    validator.validate_directory(target_dir, "Auto Folder", is_critical=True)
    
    assert len(validator.results) == 1
    assert validator.results[0].status == "PASS"
    assert "created at" in validator.results[0].message
    assert target_dir.exists()
    assert validator.is_healthy() is True


def test_run_all_checks_healthy(monkeypatch, tmp_path: Path):
    # Setup mock env so all checks pass
    monkeypatch.setenv("GEMINI_API_KEY", "fake_key")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "fake_id")
    
    # Create fake files
    env_file = tmp_path / ".env"
    env_file.touch()
    
    creds_file = tmp_path / "credentials.json"
    creds_file.touch()
    
    db_file = tmp_path / "placements.db"
    db_file.touch()

    # Modify settings instance to point to our temp paths
    settings = Settings(DATABASE_URL=f"sqlite:///{db_file}")
    settings.google_sheets_credentials_file = str(creds_file)
    
    validator = ConfigValidator(settings)
    
    # Patch the .env check path specifically
    from placement_mail_tracker.config.validator import ValidationResult

    monkeypatch.setattr(
        validator,
        "validate_file",
        lambda *_args, **_kwargs: validator.results.append(
            ValidationResult("PASS", "Fake pass", True)
        ),
    )
    
    validator.run_all_checks()
    
    # It should be healthy since we mocked the files and env vars
    assert validator.is_healthy() is True


def test_production_validation_requires_oauth_files(tmp_path: Path):
    settings = Settings(
        APP_ENV="production",
        GEMINI_API_KEY="fake_key",
        GOOGLE_SHEET_ID="fake_sheet",
        DATABASE_URL=f"sqlite:///{tmp_path / 'tracker.db'}",
        GMAIL_CREDENTIALS_FILE=str(tmp_path / "missing_gmail_credentials.json"),
        GMAIL_TOKEN_FILE=str(tmp_path / "missing_gmail_token.json"),
        GOOGLE_SHEETS_CREDENTIALS_FILE=str(tmp_path / "missing_sheets_credentials.json"),
        GOOGLE_SHEETS_TOKEN_FILE=str(tmp_path / "missing_sheets_token.json"),
    )
    validator = ConfigValidator(settings)

    validator.run_all_checks()

    assert validator.is_healthy() is False
    assert any(result.component == "gmail" for result in validator.errors())
    assert any(result.component == "sheets" for result in validator.errors())


def test_development_validation_warns_for_missing_oauth_files(tmp_path: Path):
    settings = Settings(
        APP_ENV="development",
        DATABASE_URL=f"sqlite:///{tmp_path / 'tracker.db'}",
        GMAIL_CREDENTIALS_FILE=str(tmp_path / "missing_gmail_credentials.json"),
        GMAIL_TOKEN_FILE=str(tmp_path / "missing_gmail_token.json"),
        GOOGLE_SHEETS_CREDENTIALS_FILE=str(tmp_path / "missing_sheets_credentials.json"),
        GOOGLE_SHEETS_TOKEN_FILE=str(tmp_path / "missing_sheets_token.json"),
    )
    validator = ConfigValidator(settings)

    validator.run_all_checks()

    assert validator.is_healthy() is True
    assert any(result.component == "gmail" for result in validator.warnings())


def test_calendar_check_skipped_when_sync_disabled(tmp_path: Path):
    settings = Settings(
        DATABASE_URL=f"sqlite:///{tmp_path / 'tracker.db'}",
        CALENDAR_SYNC_ENABLED=False,
    )
    validator = ConfigValidator(settings)

    validator.run_all_checks()

    assert not any(result.component == "calendar" for result in validator.results)


def test_calendar_token_missing_is_warning_not_error(tmp_path: Path):
    creds_file = tmp_path / "credentials.json"
    creds_file.touch()
    settings = Settings(
        APP_ENV="production",
        GEMINI_API_KEY="fake_key",
        GOOGLE_SHEET_ID="fake_sheet",
        DATABASE_URL=f"sqlite:///{tmp_path / 'tracker.db'}",
        GMAIL_CREDENTIALS_FILE=str(creds_file),
        GMAIL_TOKEN_FILE=str(tmp_path / "gmail_token.json"),
        GOOGLE_SHEETS_CREDENTIALS_FILE=str(creds_file),
        GOOGLE_SHEETS_TOKEN_FILE=str(tmp_path / "sheets_token.json"),
        CALENDAR_SYNC_ENABLED=True,
        CALENDAR_TOKEN_FILE=str(tmp_path / "missing_calendar_token.json"),
    )
    settings.gmail_credentials_file = str(creds_file)
    validator = ConfigValidator(settings)

    validator.run_all_checks()

    calendar_results = [r for r in validator.results if r.component == "calendar"]
    assert any(r.status == "WARNING" for r in calendar_results)
    assert not any(r.status == "ERROR" for r in calendar_results)
