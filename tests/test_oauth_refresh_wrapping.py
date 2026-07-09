"""ADR-D8 / Decision 4: wrapped refresh() + OAuth-dead alert, both stacks.

Covers:
- credentials.refresh() raising RefreshError -> typed auth error, not a raw
  google.auth exception.
- run_local_server is never launched on a non-interactive (scheduled) run.
- the one-shot alert dedup (auth_alerts module).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError

from placement_mail_tracker.gmail.gmail_client import GmailAuthenticationError, GmailClient
from placement_mail_tracker.reliability import auth_alerts
from placement_mail_tracker.sheets.sheets_sync import GoogleSheetsSync, SheetsAuthenticationError


@pytest.fixture(autouse=True)
def _isolate_alert_state(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_alerts, "_STATE_FILE", tmp_path / "oauth_alert_state.json")
    # Never let a real SMTP connection attempt happen from these tests.
    monkeypatch.setattr(
        "placement_mail_tracker.reliability.auth_alerts.EmailNotifier.send_email",
        lambda self, subject, body, is_html=False: True,
    )


def _expired_credentials():
    creds = MagicMock()
    creds.valid = False
    creds.expired = True
    creds.refresh_token = "refresh-token"
    return creds


class TestGmailRefreshWrapping:
    def test_refresh_error_raises_typed_auth_error(self, mock_settings, tmp_path):
        client = GmailClient(mock_settings)
        client.token_path = tmp_path / "token.json"
        creds = _expired_credentials()
        creds.refresh.side_effect = RefreshError("invalid_grant")

        with patch.object(GmailClient, "_load_token", return_value=creds):
            with pytest.raises(GmailAuthenticationError, match="OAuth dead"):
                client.authenticate()

    def test_no_token_non_interactive_fails_fast_without_local_server(
        self, mock_settings, tmp_path
    ):
        client = GmailClient(mock_settings)
        client.token_path = tmp_path / "token.json"
        client.credentials_path = tmp_path / "credentials.json"
        client.credentials_path.write_text("{}", encoding="utf-8")

        isatty_target = "placement_mail_tracker.gmail.gmail_client.sys.stdin.isatty"
        with patch.object(GmailClient, "_load_token", return_value=None), \
             patch(isatty_target, return_value=False), \
             patch("placement_mail_tracker.gmail.gmail_client.InstalledAppFlow") as flow_cls:
            with pytest.raises(GmailAuthenticationError, match="not interactive"):
                client.authenticate()
            flow_cls.from_client_secrets_file.assert_not_called()


class TestSheetsRefreshWrapping:
    def test_refresh_error_raises_typed_auth_error(self, mock_settings, tmp_path):
        sync = GoogleSheetsSync(mock_settings)
        sync.token_path = tmp_path / "token.json"
        creds = _expired_credentials()
        creds.refresh.side_effect = RefreshError("invalid_grant")

        with patch.object(GoogleSheetsSync, "_load_token", return_value=creds):
            with pytest.raises(SheetsAuthenticationError, match="OAuth dead"):
                sync.authenticate()

    def test_no_token_non_interactive_fails_fast_without_local_server(
        self, mock_settings, tmp_path
    ):
        sync = GoogleSheetsSync(mock_settings)
        sync.token_path = tmp_path / "token.json"
        sync.credentials_path = tmp_path / "credentials.json"
        sync.credentials_path.write_text("{}", encoding="utf-8")

        isatty_target = "placement_mail_tracker.sheets.sheets_sync.sys.stdin.isatty"
        with patch.object(GoogleSheetsSync, "_load_token", return_value=None), \
             patch(isatty_target, return_value=False), \
             patch("placement_mail_tracker.sheets.sheets_sync.InstalledAppFlow") as flow_cls:
            with pytest.raises(SheetsAuthenticationError, match="not interactive"):
                sync.authenticate()
            flow_cls.from_client_secrets_file.assert_not_called()


class TestOAuthAlertDedup:
    def test_alert_sent_once_then_suppressed(self, mock_settings, tmp_path, monkeypatch):
        monkeypatch.setattr(auth_alerts, "_STATE_FILE", tmp_path / "state.json")
        sent_count = 0

        def fake_send_email(self, subject, body, is_html=False):
            nonlocal sent_count
            sent_count += 1
            return True

        with patch(
            "placement_mail_tracker.reliability.auth_alerts.EmailNotifier.send_email",
            fake_send_email,
        ):
            auth_alerts.alert_oauth_dead_once("Gmail", "token dead", mock_settings)
            auth_alerts.alert_oauth_dead_once("Gmail", "token dead", mock_settings)

        assert sent_count == 1

    def test_clear_allows_a_new_alert_later(self, mock_settings, tmp_path, monkeypatch):
        monkeypatch.setattr(auth_alerts, "_STATE_FILE", tmp_path / "state.json")
        sent_count = 0

        def fake_send_email(self, subject, body, is_html=False):
            nonlocal sent_count
            sent_count += 1
            return True

        with patch(
            "placement_mail_tracker.reliability.auth_alerts.EmailNotifier.send_email",
            fake_send_email,
        ):
            auth_alerts.alert_oauth_dead_once("Gmail", "token dead", mock_settings)
            auth_alerts.clear_oauth_alert("Gmail")
            auth_alerts.alert_oauth_dead_once("Gmail", "token dead", mock_settings)

        assert sent_count == 2
