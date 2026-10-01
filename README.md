# Japan flight tracker — local Windows execution

Checks DFW–HND and DFW–NRT round trips for **3 adults, economy, at most one stop each way**, using Duffel. Date pairs: October 1–16, 2–17, 8–23, and 9–24, 2027. The tracker only searches; it never purchases flights.

## Where it runs

The program runs on your Windows computer every two hours through the `Japan Flight Tracker (Local)` scheduled task, plus at sign-in. Your computer must be powered on, awake, connected to the internet, and signed into your Windows account. Missed checks run when available; there is no cloud fallback. Task execution is hidden and does not require administrator privileges. A check takes up to several minutes because it searches eight round trips.

GitHub stores the code. `.github/workflows/flight-tracker.yml` is an **optional manual, offline validation workflow only**, with no schedule and no searches. GitHub Actions secrets cannot be read by a program running on your computer.

## Local credentials

Copy `.env.example` to `.env` and fill in these exact names:

| Name | Value |
| --- | --- |
| `DUFFEL_ACCESS_TOKEN` | Live Duffel API access token; use a USD account |
| `TELEGRAM_BOT_TOKEN` | Token from Telegram's BotFather |
| `TELEGRAM_CHAT_ID` | Chat ID that should receive alerts; start the bot first |
| `GOOGLE_SHEET_ID` | Spreadsheet ID between `/d/` and `/edit` in its URL |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full service-account JSON, on one line inside single quotes |

Alternatively use `GOOGLE_SERVICE_ACCOUNT_FILE` with an absolute path to a service-account JSON file stored privately on this computer. Enable the Google Sheets API in that account's Google Cloud project, then share the spreadsheet with the JSON's `client_email` as **Editor**. The program creates a `Flight History` tab if needed.

These are also the exact GitHub secret names if you choose to use manual validation there later; the local file alternative is only for your computer. Do not put credentials in `config.json` or commit `.env`, JSON keys, SQLite data, or logs. `.gitignore` excludes these local files. Credentials are loaded from the local `.env` on each run; no reinstall is needed after adding them.

Without Duffel credentials, execution validates configuration, reports missing names, skips searches and exits successfully. With Duffel configured, searches run even if Telegram or Sheets credentials are missing; history and pending alerts remain in the local database. Invalid credentials or service failures report an error and exit nonzero rather than pretending live tracking succeeded.

## Daily prices and alerts

Each tracking day starts at **8:00 AM America/Chicago**, including daylight-saving changes. The first eligible fare observed on or after that boundary is the starting fare for each destination/date pair. If the computer was unavailable at 8:00 AM, the first later observation becomes that day's starting fare. Historical data is retained. The program tracks both each itinerary's low and the overall daily low.

Fares include the offer's total for all three adults. Per-person values divide that total by three; display values round to cents, while thresholds use the unrounded amount. Only unexpired, live USD offers with economy cabin for all travelers and up to two flight segments per direction qualify. Non-USD, test, expired, and unsuitable offers cannot trigger alerts.

A Telegram alert is queued when a newly observed daily low is **$900/person or less**, or **20% or more below that itinerary's daily starting fare**. The first observation can alert at $900 or less. Repeated identical fares and rebounds do not resend alerts. Lower qualifying fares can alert again. Alerts show trip dates, total price, day low, operating carriers, offer ID and expiry. Duffel quotes expire; these are notifications, not guaranteed bookable prices or booking links.

Airlines open schedules at different times. These October 2027 trips may initially return no fares or a supplier/date-range error. The tracker will continue checking every two hours. All offer pages are examined; currencies are never mixed or converted. Search costs and limits depend on your Duffel account.

## History and reliability

`data/tracker.sqlite3` retains daily baselines, lows, history and delivery queues across runs and restarts. Google Sheets receives one row per itinerary per check, including no-offer and API-error checks. Stable row IDs prevent duplicate Sheets rows after a failed append retry. Telegram and Sheets delivery failures stay queued for the next run. Telegram can duplicate a notification if the server accepted it immediately before a network timeout or local crash; the Bot API has no idempotency key.

Local logs rotate in `logs/tracker.log`. Secrets and authenticated URLs are not logged. An OS file lock prevents overlapping checks. Keep the database if you move or reinstall the tracker. Pending alerts preserve the original observation and expiry, so a retried alert may describe an expired quote.

## Run, validate, test and schedule

From this folder in PowerShell:

```powershell
# Initial setup if installing on another computer (Python 3.12+ required):
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env

# Offline validation, tests, and one actual local check:
.\.venv\Scripts\python.exe tracker.py --validate
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tracker.py

# Install the local two-hour schedule once:
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-schedule.ps1

# Inspect, run immediately, or disable:
Get-ScheduledTaskInfo -TaskName 'Japan Flight Tracker (Local)'
Start-ScheduledTask -TaskName 'Japan Flight Tracker (Local)'
Disable-ScheduledTask -TaskName 'Japan Flight Tracker (Local)'
```

The schedule installer refuses to overwrite an existing task. If relocating the folder, remove the old task intentionally before reinstalling. Reset timing is handled in Python using Chicago time, independent of the Windows clock's displayed timezone.

Reference documentation: [Duffel offer requests](https://duffel.com/docs/api/v2/offer-requests), [Duffel offers](https://duffel.com/docs/api/v2/offers), [Google Sheets service-account sharing](https://docs.gspread.org/en/master/oauth2.html), [Telegram Bot API](https://core.telegram.org/bots/api), [Windows scheduled task triggers](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasktrigger).
