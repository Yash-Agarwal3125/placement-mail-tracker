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
    settings = Settings()
    settings.google_sheets_credentials_file = str(creds_file)
    settings.database_path = str(db_file)
    
    validator = ConfigValidator(settings)
    
    # Patch the .env check path specifically
    monkeypatch.setattr(validator, "validate_file", lambda f, d, is_critical=True: validator.results.append(
        __import__('placement_mail_tracker.config.validator', fromlist=['ValidationResult']).config.validator.ValidationResult("PASS", "Fake pass", True)
    ))
    
    validator.run_all_checks()
    
    # It should be healthy since we mocked the files and env vars
    assert validator.is_healthy() is True
