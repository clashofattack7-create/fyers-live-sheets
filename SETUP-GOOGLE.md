> **STATUS (2026-09-02): ALREADY DONE AUTOMATICALLY.**
> The whole Google Cloud setup was completed programmatically: project
> fyers-live-sheets, Sheets+Drive+IAM APIs enabled, service account
> fyers-sheets-writer created, key JSON saved at service-account.json, and
> the spreadsheet shared to the service account. The live loop is writing
> to it every 5s. This guide is kept for reference/re-creation.

# Google Sheets credentials - step-by-step (one-time, ~5 min)

The pipeline (fyers_live_sheets.py) needs a Google **service account** JSON key.
You create it in Google Cloud Console - only you can, because it requires
signing in with your own Google account.  Do exactly this:

## 1. Project
- Open https://console.cloud.google.com and sign in with your Google account.
- Top bar (blue) -> click the project dropdown -> **New Project**:
  Name: fyers-live-sheets (any name works) -> **Create**.
- Make sure the new project is selected in the top dropdown.

## 2. Enable APIs (2 clicks)
- Left menu -> **APIs & Services -> Library**.
- Search: Google Sheets API -> click it -> **Enable**.
- Search: Google Drive API -> click it -> **Enable**.

## 3. Service account + key
- Left menu -> **APIs & Services -> Credentials**.
- **Create credentials -> Service account**:
  - Name: fyers-sheets-writer
  - Role / email: skip (no role needed)
  - Click **Create and continue** then **Done**.
- In the service-account list, click the new account row.
- Tab **Keys** -> **Add key -> Create new key** -> **JSON** -> **Create**.
  A file downloads: e.g. fyers-live-sheets-<projectid>-<hash>.json.

## 4. Put the file where the pipeline watches (hot-wire)
- Copy/move that JSON to:
  D:\dsh\DSH\fyers-live-sheets\service-account.json
  (exact name - the live loop checks this path every cycle and switches on.)

That's it for the credentials.  The live run is already going: the moment the
file appears, within one 5-second cycle the pipeline will:
1. open / create the spreadsheet "BANKNIFTY Option Chain LIVE (Fyers)",
2. print its URL (watch the run console output),
3. start updating the tab ATM5-OTM5 every 5 seconds.

## 5. (Optional but recommended) viewing the sheet from your own Google account
The spreadsheet owner is the service account.  To see it in your own Google
Drive / Sheets:
- Wait for the URL in the console output (or copy it after it appears).
- Open the URL; Google will say "you need access" -> **Request access** (or
  right-click in Drive -> share the file with your own Gmail address as
  **Editor**).

## 6. Verify
Run:  python D:\dsh\DSH\fyers-live-sheets\fyers_live_sheets.py status
creds file exists : yes  ->  and the run console shows [sheet] cycles.

## Notes / troubleshooting
- If the pipeline printed "created new spreadsheet: <url>" while you were
  sharing, paste that URL into config.json under google -> spreadsheet_url
  (or spreadsheet_id) so future runs reuse the SAME sheet instead of a new one.
- Wrong JSON (e.g. the OAuth-client download, not the service-account key)
  -> gspread errors with "could not determine service account".  Re-check
  step 3 (Keys -> JSON on the service account row).
- The service-account JSON is a secret: do not share it, and do not put it
  into any git repo (this folder is intentionally not under git).