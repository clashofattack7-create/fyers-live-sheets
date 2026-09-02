# Fyers → Google Sheets — LIVE Option Chain (ATM ± 5)

BANKNIFTY ka live option chain Fyers official API se pull karke Google Sheet
par har few seconds update hota hai: **5 OTM above + ATM + 5 OTM below**
(11 strikes, CE + PE side-by-side, IV/Greeks/OI/PCR).

- Fyers token: yea machine pe already cache hai
  (`D:\dsh\DSH\option-chain-live\.fyers_credentials.json`) — `option-chain-live`
  package se auto-load hota hai.
- Sheets: `gspread` + Google **service account** JSON.

## Prerequisites (already done on this PC)

```powershell
python -m pip install gspread google-auth
# option-chain-live (Fyers client) - editable install:
python -m pip install -e D:\dsh\DSH\option-chain-live
```

## Steps

### 1. Fyers token (agar expired)

Fyers ne refresh-token API **disable** kar di hai (SEBI compliance) — token
expire hone par full re-login chahiye:

```powershell
python -m option_chain_live fyers-auth --app-id HU97KUI4I4-200 --secret <SECRET_ID> --client-id YA38754 --pin <PIN>
# (secret id apne myapi.fyers.in -> My Apps dashboard me milega)
```

### 2. Google service account (ek baar karna hai)

1. [console.cloud.google.com](https://console.cloud.google.com) → project banao.
2. **APIs & Services → Library → Enable**: `Google Sheets API` (+ `Google Drive API`,
   agar spreadsheet script khud banaye).
3. **Credentials → Create credentials → Service account** → create.
4. Service account me jaake **Keys → Add key → JSON** → download karke is folder
   me rakho (e.g. `service-account.json`). Kisi ko share mat karna — ye secret hai.
5. `config.json` me path daalo:
   ```json
   "google": { "credentials_file": "D:\\dsh\\DSH\\fyers-live-sheets\\service-account.json" }
   ```

### 3. Spreadsheet

Do option hai:

- **Auto-create**: `credentials_file` set karke `init` chalao — script
  spreadsheet khud banata hai (owner = service account). Phir isko dekhne ke
  liye us URL ko apne Google account ke saath khologe, ya
  `spreadsheet_url`/`spreadsheet_id` me existing sheet daalo + us sheet ko
  service-account email (JSON file ke `client_email`) ko **Editor** bana ke
  share karo. (Automatically create hone par script apne saath hi paste ho
  jata hai, bahar se dekhne ke liye share karna padta hai.)

Saaf karke bolo: `init` chalaane ke baad URL paste karta hai; us URL ko apne
Google account se kholne ke liye service account → sheet me **Share → apni
email** add karo (Editor).

**Ab hot-wire hai:** credentials na hone par bhi `run` chal jaata hai (mock
mode - live Fyers data pulls hota hai, snapshot `logs\latest-snapshot.csv`
me) aur har cycle me `service-account.json` check hota hai - file dikhte hi
bahi real Google Sheets writes shuru ho jaati hain, bina kisi restart ke.
Step-by-step Google setup: `SETUP-GOOGLE.md`.

### 4. Run

```powershell
python fyers_live_sheets.py status      # token + config check
python fyers_live_sheets.py init        # sheet/tab ready + headers
python fyers_live_sheets.py snapshot    # ek pull + ek write (test)
python fyers_live_sheets.py run         # LIVE loop (5s default)
python fyers_live_sheets.py run --interval 3 --max-cycles 5   # test limit
```

Ya double-click `run_live.bat`.

## Sheet layout (tab: `ATM5-OTM5`)

| Col | Meaning |
|-----|---------|
| A | Time (IST) |
| B | Spot / Index |
| C | Expiry |
| D | Strike |
| E | Moneyness (ATM/OTM/ITM) |
| F–O | CE: LTP, Bid, Ask, Vol, OI, ΔOI, IV%, Delta, Gamma, Theta |
| P–Y | PE: LTP, Bid, Ask, Vol, OI, ΔOI, IV%, Delta, Gamma, Theta |
| Z | Strike PCR (PE OI / CE OI) |

Neeche window summary: Total CE/PE OI, PCR, Max pain, expiry, fetch time.

Har cycle ki one-line local log: `logs\pull-log.csv`.

## Notes

- **Rate limit**: Fyers options-chain endpoint ko >1 req/sec pe mat chalao —
  `--interval` minimum 2s rakha hai; default 5s.
- **Market hours**: NSE closed ho to last session ki values dikhengi (Fyers
  status hi authoritative hai). 09:15–15:30 IST pe hi real-time.
- Token expiry JWT claims se li jaati hai (accurate). Refresh API Fyers ne
  DISABLE kar di hai (verified: HTTP 400 code -16 SEBI) - isliye token expire
  hone par script khud browser re-login karta hai (fyers-auth) agar
  `.secrets.env` me FYERS_SECRET + FYERS_PIN hain (`fyers_live_sheets.py
  status` → re-auth secrets: configured). Manual: `fyers-auth` command.

## Files

- `fyers_live_sheets.py` — main script (fetch → build → write loop)
- `config.json` — symbol, strikes, interval, Google/Fyers paths
- `logs\pull-log.csv` — local audit trail (auto)