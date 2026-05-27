# Placement Mail Tracker

Placement Mail Tracker is a beginner-friendly Python project skeleton for tracking placement and internship emails from Gmail.

The initial version sets up the project architecture, configuration, logging, SQLite connectivity, and a reusable Gmail API client.

## Planned Features

- Read emails from Gmail using the Gmail API and OAuth2
- Filter placement and internship emails
- Extract structured data using Google Gemini
- Store extracted records in SQLite
- Sync important records to Google Sheets
- Send Telegram notifications for new updates
- Run automatically every few hours
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

6. Run the starter application.

```bash
python -m placement_mail_tracker.app
```

## Current Status

Gmail API integration, placement filtering, and Gemini extraction scaffolding are implemented. Google Sheets and Telegram are still placeholders.

## Future Automation

The `.github/workflows/ci.yml` file is included as a starting point for future GitHub Actions checks.
