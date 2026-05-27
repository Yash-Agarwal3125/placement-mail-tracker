# 📖 Placement Mail Tracker - User Manual & Operations Guide

Welcome to the **Placement Mail Tracker** operations manual. This guide is designed to take you from absolute scratch to a fully automated, self-healing personal placement and internship tracking system on your local machine.

---

## 🛠️ Step 0: Prerequisites & Initial Setup

Before setting up the project, make sure you have the following ready:

1. **Python 3.12+**: Make sure Python is added to your system `PATH`.
2. **Google Cloud Console Account**: Required to download your Gmail/Sheets OAuth credentials.
3. **Gmail Account with 2-Step Verification**: Required to create a secure SMTP App Password for personal email alerts.
4. **Google Sheets spreadsheet**: Create a blank Google Sheet where placement records will be synced.

---

## 🚀 Step 1: Installation & Directory Setup

Open your PowerShell terminal and run the following command sequence to set up your virtual environment and install dependencies:

```powershell
# 1. Create a clean virtual environment
python -m venv .venv

# 2. Activate the virtual environment
.venv\Scripts\Activate.ps1

# 3. Install core dependencies and RapidFuzz
pip install -r requirements.txt
```

### 📁 Directory Layout Check
Ensure your folder layout is structured as follows. Missing directories (`data/`, `logs/`, `config/`) will be created automatically on the first execution:
```text
placement-mail-tracker/
├── config/
│   └── credentials.json       # <-- Downloaded Google OAuth Credentials
├── data/
│   ├── placement_mail_tracker.db  # <-- Auto-generated SQLite Database
│   └── trusted_senders.json       # <-- Discovered Institutional Senders
├── logs/
│   └── tracker.log            # <-- Running Sync Execution Logs
├── scripts/
│   ├── run_audit.py           # <-- System Verification Runner
│   └── test_email_notification.py # <-- Test SMTP Notification script
├── tests/
│   └── test_e2e_pipeline.py   # <-- Full E2E Pipeline Integration Test
├── .env                       # <-- Environment Secrets Config
├── main.py                    # <-- Main Execution Script
└── pyproject.toml
```

---

## ⚙️ Step 2: Environment Configuration (`.env`)

Copy `.env.example` to a new file named `.env` in the project root:

```powershell
cp .env.example .env
```

Open `.env` in your text editor and configure it using the details below:

```ini
APP_ENV=production
LOG_LEVEL=INFO

# ---------------------------------------------------------------------------
# 1. Google Gemini AI Settings
# ---------------------------------------------------------------------------
GEMINI_API_KEY=your_gemini_api_key  # Get your API key from Google AI Studio
GEMINI_MODEL=gemini-2.5-flash

# ---------------------------------------------------------------------------
# 2. Google Sheets Configuration
# ---------------------------------------------------------------------------
GOOGLE_SHEET_ID=your_sheet_id  # The long ID from your spreadsheet's browser URL
GOOGLE_SHEET_NAME=Opportunities

# ---------------------------------------------------------------------------
# 3. Personal SMTP Email Notification Settings
# ---------------------------------------------------------------------------
# Note: Use a Gmail "App Password", NOT your regular account password.
SMTP_EMAIL=your-gmail@gmail.com
SMTP_APP_PASSWORD=abcd efgh ijkl mnop   # 16-character code (spaces allowed)
EMAIL_RECEIVER=your-recipient-email@gmail.com
```

---

## 🔒 Step 3: Google Cloud Credentials Setup

To authorize this script to safely fetch emails from your Gmail and write to your Google Sheets:

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (e.g., `Placement-Tracker`).
3. Search for and **Enable** both the **Gmail API** and the **Google Sheets API**.
4. Go to **OAuth Consent Screen**:
   - Choose **External** user type.
   - Fill in standard developer contact emails.
   - Under **Scopes**, add `.../auth/gmail.readonly` and `.../auth/spreadsheets`.
   - Add your own email under **Test Users** (Critical!).
5. Go to **Credentials** -> **Create Credentials** -> **OAuth Client ID**:
   - Choose **Desktop App** as the Application type.
   - Name it `Placement Tracker Desktop`.
   - Click **Create**, then click **Download JSON** on the confirmation screen.
6. Rename the downloaded file to exactly `credentials.json` and place it inside the `config/` folder.

---

## 🚀 Step 4: First-Time Execution & Interactive OAuth

Run the pipeline manually from your terminal for the first time:

```powershell
python main.py
```

### 🔑 What Happens during the First Run:
1. The script connects to your local `config/credentials.json`.
2. A **web browser window opens automatically** requesting permission to access your Gmail and Google Sheets.
3. Select your test email, click *Advanced -> Go to Placement Tracker (unsafe)* (this is standard for personal test apps), and grant permissions.
4. The browser will show "The authentication flow has completed. You may close this window."
5. Secure OAuth tokens will be written to `config/token.json` and `config/sheets_token.json`. **You will never have to re-authenticate again!**

---

## 🧪 Step 5: Verification & Testing

The project includes an automated suite to audit and test every aspect of the codebase.

### 1. Run the Security & Verification Audit
Run the custom-built verification tool to confirm directories, check for secrets leak risk, and run unit suites:
```powershell
python scripts/run_audit.py
```
**Expected Output:**
```text
==================================================
--- PLACEMENT MAIL TRACKER - VALIDATION & AUDIT ---
==================================================
[DIR] Checking Project Structure
[OK] Found: src/
[OK] Found: tests/
...
==================================================
--- AUDIT SUMMARY ---
==================================================
Project Structure:  [PASS]
Security & Secrets: [PASS]
Pytest Suite:       [PASS]
==================================================
```

### 2. Run the Full Test Suite manually
Execute all 121 unit, integration, and E2E pipeline mock tests:
```powershell
python -m pytest
```

### 3. Verify SMTP Email Alerts
To test SMTP authorization and verify that email notifications arrive in your inbox, run the dedicated SMTP script:
```powershell
python scripts/test_email_notification.py
```
Check your recipient inbox for a mail with the subject: `Placement Mail Tracker - SMTP Test`.

---

## ⏰ Step 6: Automated Scheduling via Windows Task Scheduler

To make this tracker a zero-touch utility that checks your inbox and updates your Sheets every 3 hours:

1. **Launch Task Scheduler**: Press `Win + R`, type `taskschd.msc`, and hit `Enter`.
2. **Create Task**: In the Actions sidebar on the right, click **Create Basic Task...**.
3. **General Settings**:
   - **Name**: `Placement Mail Tracker`
   - **Description**: `Checks Gmail, extracts placement opportunities, logs to SQLite, and updates Google Sheets.`
   - Click **Next**.
4. **Trigger**:
   - Choose **Daily** and click **Next**.
   - Keep the start time as is, and click **Next**.
5. **Action**:
   - Choose **Start a program** and click **Next**.
6. **Configure Paths (Crucial Step)**:
   - **Program/script**: Specify the absolute path to the Python executable *inside your virtual environment*:
     ```text
     C:\Users\<YourUsername>\Documents\Codex\2026-05-26\build-a-production-ready-python-project\placement-mail-tracker\.venv\Scripts\python.exe
     ```
   - **Add arguments**: `main.py`
   - **Start in**: Specify the exact absolute path to your root project directory (without quotes):
     ```text
     C:\Users\<YourUsername>\Documents\Codex\2026-05-26\build-a-production-ready-python-project\placement-mail-tracker
     ```
   - Click **Next**.
7. **Properties**:
   - Check the **Open the Properties dialog for this task when I click Finish** checkbox and click **Finish**.
8. **Configure 3-Hour Recurrence**:
   - In the Properties pop-up, go to the **Triggers** tab, select the daily trigger, and click **Edit...**.
   - Under **Advanced settings**:
     - Check **Repeat task every:** and set it to `3 hours`.
     - Set **for a duration of:** to `Indefinitely`.
     - Click **OK**.
9. **Configure Power settings**:
   - Under the **Conditions** tab, uncheck *"Start the task only if the computer is on AC power"* so that automation continues even when running on battery.
   - Click **OK** to save the automated task.

---

## 🔍 Step 7: Troubleshooting & Self-Healing

The Placement Mail Tracker includes self-healing, transactional recovery logic to resolve common issues automatically.

### 1. Re-Authenticating Google APIs
* **Symptom**: Console throws `googleapiclient.errors.HttpError` unauthorized access, or the authentication fails.
* **Resolution**:
  - Simply delete `config/token.json` and `config/sheets_token.json` from the project directory.
  - Run `python main.py` manually in your console, and log back in through the browser.

### 2. Auto-Healing Corrupted Tokens
* **Symptom**: Power failures or process interrupts truncate `config/token.json` causing permanent unhandled JSON parse exceptions.
* **Resolution**: The system automatically detects token file corruptions, deletes the corrupted file, logs a clean warning, and gracefully resets the authentication flow without crashing.

### 3. Google Sheets Downtime or Sync Failures
* **Symptom**: Sheets API is offline or rate-limiting limits are hit.
* **Resolution**: The E2E runner gracefully catches Sheets downtime. Opportunities continue to write safely to SQLite, and processed status is completed. On the very next successful scheduler cycle, the synchronizer will pull **all active records** in bulk, automatically matching and writing missing rows to Sheets.

### 4. Database Locking / Transaction Safety
* **Symptom**: SQLite database blocks or raises database is locked.
* **Resolution**: Ensure no external SQLite browser or admin panels are locking the database. The system uses strict database transactional isolation (`with self.connection:`), guaranteeing complete rollbacks on failures to prevent orphaned records.

### 5. SMTP App Password Issues
* **Symptom**: Script fails with `SMTPAuthenticationError` or network timeout during email delivery.
* **Resolution**:
  - Double-check your Google Account security settings. Make sure **2-Step Verification** is active.
  - Generate a new **App Password**, select App -> *Other*, name it `Placement-Tracker`, and copy the 16-character code into `.env` (ensure spaces are handled properly).
