# Placement Mail Tracker

Placement Mail Tracker is a lightweight Python system for tracking placement and
internship emails from Gmail, extracting structured drive details, storing them in
SQLite, syncing Google Sheets, and sending notifications.

It is designed for a personal Windows PC running Windows Task Scheduler. Each run
starts, completes one sync cycle, updates local reliability state, and exits.

## Planned Features

- Read emails from Gmail using the Gmail API and OAuth2
- Filter placement and internship emails
- Extract structured data using Google Gemini
- Store extracted records in SQLite
- Sync important records to Google Sheets
- Send Telegram and email notifications for new updates
- Run automatically every few hours through Windows Task Scheduler
- Prepare for future GitHub Actions automation

## Tech Stack

- Python 3.12+
- SQLite
- python-dotenv
- Google Gmail API
- Google Gemini API
- Google Sheets API
- Telegram Bot API

## Project Structure

```text
placement-mail-tracker/
|-- .github/
|   `-- workflows/
|       `-- ci.yml
|-- config/
|   `-- .gitkeep
|-- data/
|   `-- .gitkeep
|-- logs/
|   `-- .gitkeep
|-- scripts/
|   `-- init_db.py
|-- src/
|   `-- placement_mail_tracker/
|       |-- app.py
|       |-- config/
|       |-- db/
|       |-- filters/
|       |-- gmail/
|       |   |-- client.py
|       |   `-- gmail_client.py
|       |-- ai/
|       |-- gemini/
|       |-- models/
|       |-- notifications/
|       |-- scheduler/
|       |-- sheets/
|       `-- utils/
|-- tests/
|   |-- test_gmail_parsing.py
|   `-- test_imports.py
|-- .env.example
|-- .gitignore
|-- pyproject.toml
`-- requirements.txt
```

## Setup

1. Create and activate a virtual environment.

```bash
python -m venv .venv
```

2. Install dependencies.

```bash
pip install -r requirements.txt
```

3. Copy environment variables.

```bash
cp .env.example .env
```

4. Update `.env` with your API keys and configuration.

For Gmail, create an OAuth client in Google Cloud Console, enable the Gmail API, and place the downloaded OAuth client file at:

```text
config/credentials.json
```

The first Gmail run opens a browser for OAuth consent and saves the local token to:

```text
config/token.json
```

Both JSON files are ignored by git.

5. Initialize the SQLite database.

```bash
python scripts/init_db.py
```

6. Run one sync cycle.

```bash
python main.py
```

## Runtime Model

The production runner is intentionally simple:

```text
Windows Task Scheduler
-> python main.py
-> validate config
-> acquire local lock
-> fetch Gmail
-> process drives
-> sync Google Sheets
-> send notifications
-> update heartbeat/state
-> exit
```

The project does not use APScheduler, Celery, Redis, RabbitMQ, background
workers, infinite loops, or a web server.

## Environment Modes

Set `APP_ENV` in `.env`.

- `development`: missing Gmail, Sheets, or Gemini credentials are warnings so
  beginner setup and local tests can continue.
- `testing`: behaves like development for credentials and is intended for
  automated tests.
- `production`: validates required Gmail, Sheets, Gemini, database, and `.env`
  settings at startup. Missing required credentials fail fast and return a
  failed run status.

Exit codes:

- `0`: success
- `1`: failed
- `2`: partial success

## Reliability Files

- `logs/app.log`: rotating application log, 10 MB max per file, 5 backups.
- `data/system_health.json`: tracks `last_success`, `last_failure`,
  `consecutive_failures`, and whether the current failure streak was alerted.
- `data/heartbeat.json`: updated only after a successful sync and includes the
  last successful run, processed message count, created/updated drives, and
  final status.

If the last successful heartbeat is older than `HEARTBEAT_INACTIVITY_HOURS`
defaulting to 6, the next scheduled run logs an inactivity warning. If
`consecutive_failures` reaches `FAILURE_ALERT_THRESHOLD` defaulting to 3, the
runner sends one failure alert to `NOTIFICATION_EMAIL` or `EMAIL_RECEIVER` for
that failure streak.

## Current Status

Gmail API integration, placement filtering, Gemini extraction, SQLite storage,
Google Sheets sync, notification scaffolding, one-shot runner reliability,
heartbeat tracking, failure alerting, and log rotation are implemented.

## Future Automation

The `.github/workflows/ci.yml` file is included as a starting point for future GitHub Actions checks.
