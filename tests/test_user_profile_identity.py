"""Doc 15 §3.1: identity matching must refuse on a fallback or reg-no-less profile."""

import json

from placement_mail_tracker.config.user_profile import UserProfile


def test_missing_file_is_default_and_unverified(tmp_path):
    profile = UserProfile.load(str(tmp_path / "does_not_exist.json"))
    assert profile.is_default is True
    assert profile.is_identity_verified() is False


def test_unparseable_file_is_default_and_unverified(tmp_path):
    path = tmp_path / "user_profile.json"
    path.write_text("not json", encoding="utf-8")
    profile = UserProfile.load(str(path))
    assert profile.is_default is True
    assert profile.is_identity_verified() is False


def test_real_profile_without_registration_no_is_unverified(tmp_path):
    path = tmp_path / "user_profile.json"
    path.write_text(
        json.dumps(
            {
                "degree": "B.Tech",
                "branch": "Computer Science",
                "campus": "Vellore",
                "graduation_year": 2027,
                "cgpa": 8.5,
            }
        ),
        encoding="utf-8",
    )
    profile = UserProfile.load(str(path))
    assert profile.is_default is False
    assert profile.is_identity_verified() is False


def test_real_profile_with_registration_no_is_verified(tmp_path):
    path = tmp_path / "user_profile.json"
    path.write_text(
        json.dumps(
            {
                "degree": "B.Tech",
                "branch": "Computer Science",
                "campus": "Vellore",
                "graduation_year": 2027,
                "cgpa": 8.5,
                "full_name": "Yash Agarwal",
                "registration_no": "23BAI1234",
            }
        ),
        encoding="utf-8",
    )
    profile = UserProfile.load(str(path))
    assert profile.is_default is False
    assert profile.is_identity_verified() is True
