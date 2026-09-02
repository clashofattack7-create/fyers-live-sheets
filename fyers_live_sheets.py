# -*- coding: utf-8 -*-
r"""
fyers_live_sheets.py - LIVE Fyers option chain -> Google Sheets (ATM +/- N strikes).

Pulls the official Fyers options-chain-v3 feed through the local
'option-chain-live' package and rewrites a Google Sheet tab every 'interval'
seconds: one row per strike, CE and PE side by side, plus a window summary.

Auth: the Fyers refresh-token API is DISABLED by Fyers (SEBI regulation,
verified: code -16).  Access-token expiry is taken from the JWT claims, and
when the token dies the pipeline launches an automatic browser re-login
(fyers-auth) using FYERS_SECRET / FYERS_PIN from .secrets.env.

Google: needs a service-account JSON.  Until it appears at
google.credentials_file the pipeline runs in MOCK mode - it pulls live Fyers
data every cycle, writes the snapshot to logs\latest-snapshot.csv and checks
for the key file each cycle, then hot-wires real Google Sheets writes with no
restart.

Commands
--------
  python fyers_live_sheets.py init                  # connect/create sheet, write headers
  python fyers_live_sheets.py snapshot              # one pull + one write (test)
  python fyers_live_sheets.py run                   # live loop until Ctrl+C
  python fyers_live_sheets.py run --max-cycles 3    # bounded live test
  python fyers_live_sheets.py fyers-refresh --pin XXXX
  python fyers_live_sheets.py status
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import gspread

# This machine's DNS returns IPv6-only addresses for several googleapis hosts
# while IPv6 is unreachable here, so all Google API calls must go over IPv4.
import socket as _socket
_orig_gai = _socket.getaddrinfo
def _v4_only(host, port, *a, **k):
    res = _orig_gai(host, port, *a, **k)
    v4 = [r for r in res if r[0] == _socket.AF_INET]
    return v4 or res
_socket.getaddrinfo = _v4_only
_socket.setdefaulttimeout(30)

from option_chain_live.fyers_client import (
    FyersClient,
    FyersCredentials,
    FyersError,
    jwt_exp,
)
from option_chain_live.fyers_chain import parse_chain
from option_chain_live.models import OptionChain, ChainRow

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SECRETS_FILE = os.path.join(BASE_DIR, ".secrets.env")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "pull-log.csv")
SNAPSHOT_CSV = os.path.join(LOG_DIR, "latest-snapshot.csv")

IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = [
    "Time (IST)", "Spot", "Expiry", "Strike", "Moneyness",
    "CE LTP", "CE Bid", "CE Ask", "CE Vol", "CE OI", "CE dOI", "CE IV%",
    "CE Delta", "CE Gamma", "CE Theta",
    "PE LTP", "PE Bid", "PE Ask", "PE Vol", "PE OI", "PE dOI", "PE IV%",
    "PE Delta", "PE Gamma", "PE Theta",
    "PCR",
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "symbol": "NSE:BANKNIFTY-INDEX",
    "strikes_per_side": 5,          # ATM + 5 below + 5 above (ATM+/-5 window)
    "interval_seconds": 5,
    "fyers": {
        "token_file": r"D:\dsh\DSH\option-chain-live\.fyers_credentials.json",
        "secrets_file": r"D:\dsh\DSH\fyers-live-sheets\.secrets.env",
    },
    "google": {
        # Service-account JSON - create it once (README.md step 2) and drop it
        # at this path; the live loop notices it by itself and starts writing.
        "credentials_file": r"D:\dsh\DSH\fyers-live-sheets\service-account.json",
        "user_token_file": r"D:\dsh\DSH\fyers-live-sheets\.google-token.json",   # preferred: sheets owned by your own Google account
        "spreadsheet_id": "",        # leave "" to let the script create one
        "spreadsheet_url": "",       # or the share link of an existing sheet
        "spreadsheet_title": "BANKNIFTY Option Chain LIVE (Fyers)",
        "worksheet_title": "ATM5-OTM5",
    },
}

# ---------------------------------------------------------------- config ----

def load_config(path: str = CONFIG_FILE) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save_config(cfg: Dict[str, Any], path: str = CONFIG_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- secrets ---

def load_secrets(path: str = SECRETS_FILE) -> Dict[str, str]:
    """Parse .secrets.env (KEY=VALUE lines, # comments)."""
    out: Dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def apply_secrets(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Expose secrets as env vars (without overriding real env) and return them."""
    sec = load_secrets((cfg.get("fyers") or {}).get("secrets_file") or SECRETS_FILE)
    for k, v in sec.items():
        if v and not os.environ.get(k):
            os.environ[k] = v
    return sec


# ---------------------------------------------------------------- fyers -----

def make_client(cfg: Dict[str, Any]) -> FyersClient:
    sec = apply_secrets(cfg)  # must run before FyersCredentials reads FYERS_PIN
    token_file = (cfg.get("fyers") or {}).get("token_file") or None
    creds = FyersCredentials(token_file=token_file)
    client = FyersClient(creds)
    client.creds.pin = sec.get("FYERS_PIN") or os.environ.get("FYERS_PIN", "")
    return client


def fetch_chain(client: FyersClient, cfg: Dict[str, Any]) -> OptionChain:
    """Pull one chain snapshot for the configured symbol + strike window."""
    symbol = cfg["symbol"]
    raw = client.option_chain_raw(symbol, strikecount=cfg.get("strikes_per_side", 5))
    chain = parse_chain(raw, symbol)
    chain.rows.sort(key=lambda r: r.strike)
    return chain


def window_rows(chain: OptionChain, n: int) -> List[ChainRow]:
    """ATM +/- n strikes (n below, ATM, n above), sorted ascending."""
    rows = chain.nearest_rows(n)
    return sorted(rows, key=lambda r: r.strike)

# ------------------------------------------------------- mock worksheet -----

def _a1rc(a1: str) -> Tuple[int, int]:
    """'A1' -> (row, col) 0-based."""
    letters = "".join(ch for ch in a1 if ch.isalpha())
    digits = "".join(ch for ch in a1 if ch.isdigit())
    col = 0
    for ch in letters.upper():
        col = col * 26 + (ord(ch) - ord("A") + 1)
    return (int(digits) - 1 if digits else 0, col - 1)


class _Cell:
    def __init__(self, value: Any):
        self.value = value


class MockWorksheet:
    """gspread Worksheet stand-in used until the Google key arrives."""

    def __init__(self, title: str = "ATM5-OTM5", rows: int = 200, cols: int = 40):
        self.title = title
        self.rows = rows
        self.cols = cols
        self._grid: List[List[Any]] = [["" for _ in range(cols)] for _ in range(rows)]
        self.updates = 0

    def acell(self, a1: str) -> _Cell:
        r, c = _a1rc(a1)
        if 0 <= r < self.rows and 0 <= c < self.cols:
            return _Cell(self._grid[r][c])
        return _Cell("")

    def update(self, values: List[List[Any]], range_label: str = "A1",
               value_input_option: str = "USER_ENTERED") -> None:
        start_r, start_c = _a1rc(range_label)
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                r, c = start_r + i, start_c + j
                if 0 <= r < self.rows and 0 <= c < self.cols:
                    self._grid[r][c] = v
        self.updates += 1

    def format(self, *a: Any, **kw: Any) -> None:  # noqa: A003 - gspread API
        pass

    def freeze(self, rows: int = 0) -> None:
        pass

    def dump_csv(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for row in self._grid:
                if any(str(v) != "" for v in row):
                    w.writerow(row)


# ---------------------------------------------------------------- sheet -----

def _google_client(cfg: Dict[str, Any]):
    """Auth: user refresh-token first (sheet owned by YOUR account), SA key as
    fallback.  Returns a gspread client or None."""
    gcfg = cfg["google"]
    # SA first: it holds the full spreadsheets+drive scope (verified working);
    # the user refresh token only has cloud-platform, which Sheets rejects.
    key_file = gcfg.get("credentials_file") or ""
    if key_file and os.path.exists(key_file):
        try:
            return gspread.service_account(filename=key_file)
        except Exception as e:
            print(f"google service-account auth failed: {e}")
    token_file = gcfg.get("user_token_file") or ""
    if token_file and os.path.exists(token_file):
        try:
            tok = json.load(open(token_file, encoding="utf-8"))
            from google.oauth2.credentials import Credentials
            creds = Credentials(
                token=None,
                refresh_token=tok["refresh_token"],
                client_id=tok.get("client_id"),
                client_secret=tok.get("client_secret"),
                token_uri=tok.get("token_uri") or "https://oauth2.googleapis.com/token",
                # gcloud OAuth client registers only cloud-platform; it covers
                # Sheets + Drive (cloud-platform was the granted consent scope).
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            return gspread.authorize(creds)
        except Exception as e:
            print(f"google user-token auth failed: {e}")
    return None


def open_sheet(cfg: Dict[str, Any]) -> Tuple[Optional[Any], Any, bool]:
    """Open/create the spreadsheet + tab. Returns (sheet, worksheet, real).

    'real' is False when no Google auth works - a MockWorksheet is returned
    so the live loop keeps running meanwhile.
    """
    try:
        gc = _google_client(cfg)
        if gc is None:
            return None, MockWorksheet(cfg["google"].get("worksheet_title") or "ATM5-OTM5"), False
        gcfg = cfg["google"]
        if gcfg.get("spreadsheet_id"):
            sh = gc.open_by_key(gcfg["spreadsheet_id"])
        elif gcfg.get("spreadsheet_url"):
            sh = gc.open_by_url(gcfg["spreadsheet_url"])
        else:
            sh = gc.create(gcfg.get("spreadsheet_title") or "option-chain-live")
            gcfg["spreadsheet_id"] = sh.id
            save_config(cfg)
            print(f"created new spreadsheet: {sh.url}")
        ws = None
        want = gcfg.get("worksheet_title") or "ATM5-OTM5"
        for s in sh.worksheets():
            if s.title == want:
                ws = s
                break
        if ws is None:
            ws = sh.add_worksheet(title=want, rows=200, cols=40)
        return sh, ws, True
    except Exception as e:
        print(f"google sheets connect error: {e}")
        return None, MockWorksheet(cfg["google"].get("worksheet_title") or "ATM5-OTM5"), False

    if gcfg.get("spreadsheet_id"):
        sh = gc.open_by_key(gcfg["spreadsheet_id"])
    elif gcfg.get("spreadsheet_url"):
        sh = gc.open_by_url(gcfg["spreadsheet_url"])
    else:
        sh = gc.create(gcfg.get("spreadsheet_title") or "option-chain-live")
        gcfg["spreadsheet_id"] = sh.id
        save_config(cfg)
        print(f"created new spreadsheet: {sh.url}")

    ws = None
    want = gcfg.get("worksheet_title") or "ATM5-OTM5"
    for s in sh.worksheets():
        if s.title == want:
            ws = s
            break
    if ws is None:
        ws = sh.add_worksheet(title=want, rows=200, cols=40)
    return sh, ws, True

def ensure_headers(ws) -> bool:
    """Write + style the header row if the sheet is empty. True if written."""
    a1 = ws.acell("A1").value
    if a1 == HEADERS[0]:
        return False
    ws.update([HEADERS], "A1", value_input_option="RAW")
    try:
        ws.format(
            "A1:Z1",
            {
                "textFormat": {"bold": True, "fontSize": 10},
                "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 0.85},
            },
        )
        ws.freeze(rows=1)
    except Exception:
        pass  # mock or picky API - cosmetic only
    return True


def _num(v: float, nd: int = 2):
    return round(float(v or 0.0), nd)


def build_snapshot_values(chain: OptionChain, rows: List[ChainRow]) -> List[List[Any]]:
    """Header + per-strike rows + summary block, as one 2D array."""
    now_ist = datetime.now(IST)
    ts = now_ist.strftime("%H:%M:%S")
    values: List[List[Any]] = [[h for h in HEADERS]]

    atm = chain.atm_row()
    atm_strike = atm.strike if atm else None

    for r in rows:
        c, p = r.calls, r.puts
        pcr = round(p.oi / c.oi, 3) if c.oi else ""
        if atm_strike is not None and abs(r.strike - atm_strike) < 1e-6:
            money = "ATM"
        elif r.moneyness == "ATM":  # model ATM band is 0.5% wide - narrow it
            money = "ITM" if r.strike < chain.spot else "OTM"
        else:
            money = r.moneyness
        values.append([
            ts, _num(r.spot, 2), chain.expiry, _num(r.strike, 0), money,
            _num(c.ltp), _num(c.bid_price), _num(c.ask_price), int(c.volume),
            int(c.oi), int(c.change_oi), round(c.iv * 100.0, 2),
            round(c.delta, 3), round(c.gamma, 4), round(c.theta, 3),
            _num(p.ltp), _num(p.bid_price), _num(p.ask_price), int(p.volume),
            int(p.oi), int(p.change_oi), round(p.iv * 100.0, 2),
            round(p.delta, 3), round(p.gamma, 4), round(p.theta, 3),
            pcr,
        ])

    values.append([""] * len(HEADERS))  # separator row

    ce_oi = sum(r.calls.oi for r in rows)
    pe_oi = sum(r.puts.oi for r in rows)
    summary: List[List[Any]] = [
        ["ATM STRIKE", _num(atm_strike, 0) if atm_strike else ""],
        ["TOTAL CE OI (window)", int(ce_oi)],
        ["TOTAL PE OI (window)", int(pe_oi)],
        ["PCR (window)", round(pe_oi / ce_oi, 3) if ce_oi else ""],
        ["MAX PAIN (window)", chain.max_pain_estimate],
        ["Expiry", chain.expiry],
        ["Fetched (UTC)", now_ist.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")],
    ]
    for k, v in summary:
        row = [k, v] + [""] * (len(HEADERS) - 2)
        values.append(row)
    return values


def write_snapshot(ws, values: List[List[Any]]) -> None:
    ws.update(values, "A1", value_input_option="USER_ENTERED")


def append_log_line(chain: OptionChain, rows: List[ChainRow], ok: bool, mode: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    new = not os.path.exists(LOG_FILE)
    ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    ce_oi = int(sum(r.calls.oi for r in rows))
    pe_oi = int(sum(r.puts.oi for r in rows))
    line = f"{ts},{ok},{mode},{_num(chain.spot,2)},{ce_oi},{pe_oi}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        if new:
            f.write("time_ist,ok,mode,spot,ce_oi,pe_oi\n")
        f.write(line)


def pull_and_write(cfg: Dict[str, Any], ws, client: FyersClient, sh=None) -> int:
    """One cycle: fetch -> build -> write. Returns number of rows written."""
    chain = fetch_chain(client, cfg)
    rows = window_rows(chain, cfg.get("strikes_per_side", 5))
    values = build_snapshot_values(chain, rows)
    write_snapshot(ws, values)
    mode = "sheet" if sh is not None else "mock"
    if mode == "mock":
        try:
            ws.dump_csv(SNAPSHOT_CSV)
        except Exception:
            pass
    append_log_line(chain, rows, ok=True, mode=mode)
    spot = _num(chain.spot, 2)
    tag = f" -> {sh.url}" if sh is not None else " -> logs\\latest-snapshot.csv (google key pending)"
    print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] [{mode}] spot={spot} "
          f"strikes={rows[0].strike:.0f}..{rows[-1].strike:.0f} "
          f"({len(rows)} strikes, ATM {chain.atm_row().strike:.0f}){tag}")
    return len(values)


# ------------------------------------------------------- auto re-auth -------

def maybe_auto_reauth(cfg: Dict[str, Any]) -> bool:
    """Launch a browser re-login (fyers-auth) when the token dies.

    Returns True if a fresh token is in place afterwards.  The browser opens
    with the .fyers-edge profile (already Cloudflare-clean); FYERS_PIN is
    typed automatically - if Fyers asks for an OTP you get ~6 minutes in the
    browser window to complete it.
    """
    sec = apply_secrets(cfg)
    if not sec.get("FYERS_SECRET") or not sec.get("FYERS_PIN"):
        return False
    app_id = sec.get("FYERS_APP_ID") or "HU97KUI4I4-200"
    client_id = sec.get("FYERS_CLIENT_ID") or "YA38754"
    print("Fyers token expired - launching browser re-login (complete any OTP in the window) ...")
    env = dict(os.environ)
    env["FYERS_PIN"] = sec["FYERS_PIN"]
    env["FYERS_CLIENT_ID"] = client_id
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "option_chain_live", "fyers-auth",
             "--app-id", app_id, "--secret", sec["FYERS_SECRET"],
             "--client-id", client_id, "--pin", sec["FYERS_PIN"],
             "--timeout-min", "6"],
            env=env, timeout=7 * 60,
        )
    except subprocess.TimeoutExpired:
        print("re-login timed out")
        return False
    if proc.returncode != 0:
        print("re-login failed")
        return False
    token_file = (cfg.get("fyers") or {}).get("token_file") or None
    try:
        creds = FyersCredentials(token_file=token_file)
        t = creds.load_tokens() or {}
        exp = t.get("expires_at") or ""
        exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        if exp_dt > datetime.now(timezone.utc) + timedelta(minutes=5):
            # verify with a real data call
            FyersClient(creds).option_chain_raw(cfg["symbol"], strikecount=1)
            print(f"re-login OK - token valid until {exp_dt.astimezone(IST)} IST")
            return True
    except Exception as e:
        print(f"re-login token check failed: {e}")
    return False

# ---------------------------------------------------------------- commands --

def cmd_init(args) -> int:
    cfg = load_config()
    sh, ws, real = open_sheet(cfg)
    ensure_headers(ws)
    if real:
        print(f"sheet ready: {sh.url}  (tab: {ws.title})")
    else:
        print("Google service-account JSON not found yet - create it and drop it at "
              f"{cfg['google']['credentials_file']} (README step 2). Pipeline will auto-wire.")
    return 0


def cmd_snapshot(args) -> int:
    cfg = load_config()
    sh, ws, real = open_sheet(cfg)
    ensure_headers(ws)
    client = make_client(cfg)
    pull_and_write(cfg, ws, client, sh=sh)
    if real:
        print(f"sheet: {sh.url}")
    return 0


def cmd_run(args) -> int:
    cfg = load_config()
    if args.interval:
        cfg["interval_seconds"] = args.interval
    if args.strikes:
        cfg["strikes_per_side"] = args.strikes
    sh, ws, real = open_sheet(cfg)
    ensure_headers(ws)
    client = make_client(cfg)
    interval = max(2, int(cfg.get("interval_seconds") or 5))
    cycles = 0
    if real:
        print(f"LIVE started: {cfg['symbol']} ATM+-{cfg.get('strikes_per_side')} "
              f"-> {sh.url}  every {interval}s. Ctrl+C to stop. (tab: {ws.title})")
    else:
        print(f"LIVE started: {cfg['symbol']} ATM+-{cfg.get('strikes_per_side')} "
              f"every {interval}s - pulling real Fyers data, sheet writes ON HOLD "
              f"until {cfg['google']['credentials_file']} exists (hot-wires by itself).")
    while args.max_cycles is None or cycles < args.max_cycles:
        if not real:
            try:
                sh, ws, real = open_sheet(cfg)  # cheap re-check every cycle
            except Exception as e:
                print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] google connect error: {e}")
                real = False
            if real:
                ensure_headers(ws)
                print(f"Google key detected - switched to live sheet: {sh.url}")
        try:
            pull_and_write(cfg, ws, client, sh=sh)
            cycles += 1
        except FyersError as e:
            msg = str(e)
            print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] fyers error: {msg}")
            if "expired" in msg.lower() or "TOKEN_EXPIRED" in msg or "invalid token" in msg.lower():
                if maybe_auto_reauth(cfg):
                    continue
                print("FIX: run 'python -m option_chain_live fyers-auth' --app-id HU97KUI4I4-200 "
                      "--secret <SECRET_ID> --client-id YA38754 --pin <PIN> "
                      "(or store FYERS_SECRET+FYERS_PIN in .secrets.env for auto re-login)")
                return 2
            time.sleep(3)
            continue
        except Exception as e:  # gspread / network wobble
            print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] sheets error: {e}")
            time.sleep(3)
            continue
        if args.max_cycles is not None:
            break
        time.sleep(interval)
    print(f"cycles completed: {cycles}")
    return 0


def cmd_fyers_refresh(args) -> int:
    cfg = load_config()
    client = make_client(cfg)
    try:
        client.refresh_token(pin=args.pin)
    except FyersError as e:
        print(f"error: {e}")
        return 1
    t = client.creds.load_tokens() or {}
    print(f"OK: token refreshed, expires {t.get('expires_at')}")
    return 0


def cmd_status(args) -> int:
    cfg = load_config()
    print(f"symbol            : {cfg['symbol']}")
    print(f"strikes per side  : {cfg.get('strikes_per_side')}  (ATM +/- window)")
    print(f"interval          : {cfg.get('interval_seconds')}s")
    fy = cfg.get("fyers", {})
    token_file = fy.get("token_file")
    print(f"fyers token file  : {token_file}")
    try:
        t = FyersCredentials(token_file=token_file).load_tokens()
    except Exception:
        t = None
    if t:
        print(f"token issued      : {t.get('issued_at')}")
        print(f"token expires     : {t.get('expires_at')}  (source: {t.get('expires_source')})")
        if t.get("refresh_expires_at"):
            print(f"refresh expires   : {t.get('refresh_expires_at')}  [API disabled by Fyers]")
        try:
            exp = t.get("expires_at") or ""
            exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            remain = exp_dt - datetime.now(timezone.utc)
            print(f"token status      : VALID for ~{remain.total_seconds() / 3600:.1f}h")
        except ValueError:
            print("token status      : (expiry unknown)")
        print("refresh API       : DISABLED by Fyers (SEBI) - auto browser re-login on expiry")
        sec = load_secrets(fy.get("secrets_file") or SECRETS_FILE)
        print(f"re-auth secrets   : {'configured' if sec.get('FYERS_SECRET') and sec.get('FYERS_PIN') else 'MISSING (manual re-login needed on expiry)'}")
    else:
        print("token status      : MISSING")
    g = cfg.get("google", {})
    key = g.get("credentials_file") or ""
    print(f"creds file        : {key}")
    print(f"creds file exists : {'yes' if key and os.path.exists(key) else 'NO - mock mode (logs\\latest-snapshot.csv)'}")
    print(f"spreadsheet id    : {g.get('spreadsheet_id') or '(auto-create on init)'}")
    print(f"spreadsheet url   : {g.get('spreadsheet_url') or ''}")
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            print(f"pull log lines    : {sum(1 for _ in f) - 1}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fyers_live_sheets",
        description="LIVE Fyers option chain -> Google Sheets (ATM +/- N strikes)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create/connect sheet + write headers").set_defaults(func=cmd_init)
    sub.add_parser("snapshot", help="one pull + one write").set_defaults(func=cmd_snapshot)

    pr = sub.add_parser("run", help="live loop until Ctrl+C")
    pr.add_argument("--interval", type=int, default=0, help="seconds between updates (min 2)")
    pr.add_argument("--strikes", type=int, default=0, help="strikes per side of ATM")
    pr.add_argument("--max-cycles", type=int, default=None, help="stop after N cycles (testing)")
    pr.set_defaults(func=cmd_run)

    pf = sub.add_parser("fyers-refresh", help="try refreshing cached Fyers token (needs PIN)")
    pf.add_argument("--pin", required=True, help="your Fyers 4-digit PIN")
    pf.set_defaults(func=cmd_fyers_refresh)

    sub.add_parser("status", help="show config + token + sheet state").set_defaults(func=cmd_status)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        if os.environ.get("FYERS_SHEETS_DEBUG"):
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())