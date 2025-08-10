import os
import json
import time
from datetime import datetime, timezone

import gspread
from alpaca_trade_api.rest import REST, TimeFrame  # TimeFrame not needed, but keeps import handy


# =========================
# Config (env or defaults)
# =========================
SHEET_NAME    = os.getenv("SHEET_NAME", "Trading Log")
SCREENER_TAB  = os.getenv("SCREENER_TAB", "screener")
LOG_TAB       = os.getenv("LOG_TAB", "log")

ALPACA_API_KEY     = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET_KEY  = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
APCA_API_BASE_URL  = os.getenv("APCA_API_BASE_URL", "https://api.alpaca.markets")  # live by default

# Buy 5% of available buying power per symbol
PERCENT_PER_TRADE  = float(os.getenv("PERCENT_PER_TRADE", "5.0"))  # percent
MIN_ORDER_NOTIONAL = float(os.getenv("MIN_ORDER_NOTIONAL", "1.00"))  # fallback floor
SLEEP_BETWEEN_ORDERS_SEC = float(os.getenv("SLEEP_BETWEEN_ORDERS_SEC", "0.5"))
EXTENDED_HOURS = os.getenv("EXTENDED_HOURS", "false").lower() in ("1", "true", "yes")


# =========================
# Helpers
# =========================
def now_iso_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_google_client():
    raw = os.getenv("GOOGLE_CREDS_JSON")
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDS_JSON env var.")
    creds = json.loads(raw)
    return gspread.service_account_from_dict(creds)


def _get_ws(gc, sheet_name, tab):
    sh = gc.open(sheet_name)
    try:
        return sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=tab, rows="2000", cols="50")


def read_screener_tickers(ws):
    """
    Reads the screener tab and returns a list of tickers.
    Assumes a header row containing a column named 'Ticker'.
    """
    values = ws.get_all_values()
    if not values:
        return []

    header = [h.strip() for h in values[0]]
    try:
        idx = header.index("Ticker")
    except ValueError:
        # Fallback: assume first column
        idx = 0

    tickers = []
    for row in values[1:]:
        if idx < len(row):
            t = row[idx].strip().upper()
            if t:
                tickers.append(t)

    # Preserve order but remove dups while keeping first occurrence
    seen = set()
    ordered = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def append_logs(ws, rows):
    """
    rows: List[List[Any]] to append to LOG_TAB.
    """
    if not rows:
        return
    # Ensure a basic header exists (idempotent-ish)
    existing = ws.get_all_values()
    if not existing:
        ws.append_row(["Timestamp", "Action", "Symbol", "NotionalUSD", "Qty", "OrderID", "Status", "Note"])
    # Append in batches
    for i in range(0, len(rows), 100):
        ws.append_rows(rows[i:i+100], value_input_option="USER_ENTERED")


def make_alpaca():
    if not (ALPACA_API_KEY and ALPACA_SECRET_KEY):
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")
    return REST(key_id=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY, base_url=APCA_API_BASE_URL)


def place_buy_notional(api: REST, symbol: str, notional: float, extended: bool):
    """
    Places a market buy with notional dollars. Returns the order object.
    This intentionally ignores existing positions and open orders (per requirements).
    """
    # Alpaca expects strings for some numeric fields; floats are okay too.
    order = api.submit_order(
        symbol=symbol,
        side="buy",
        type="market",
        time_in_force="day",
        notional=round(notional, 2),
        extended_hours=extended,
    )
    return order


# =========================
# Main
# =========================
def main():
    print("🚀 Buy bot starting")

    # Connect
    gc = get_google_client()
    api = make_alpaca()

    # Sheets
    screener_ws = _get_ws(gc, SHEET_NAME, SCREENER_TAB)
    log_ws = _get_ws(gc, SHEET_NAME, LOG_TAB)

    # Read symbols to buy
    symbols = read_screener_tickers(screener_ws)
    if not symbols:
        print("ℹ️ Screener has no tickers to buy. Exiting.")
        return

    # Loop & buy 5% of CURRENT buying power for each symbol
    logs = []
    for i, symbol in enumerate(symbols, 1):
        try:
            # Refresh account each time so we never overspend
            acct = api.get_account()
            # Depending on account type/margin setting, use buying_power; fall back to cash
            try:
                buying_power = float(acct.buying_power)
            except Exception:
                buying_power = float(acct.cash)

            notional = buying_power * (PERCENT_PER_TRADE / 100.0)
            if notional < MIN_ORDER_NOTIONAL:
                note = f"Skipped: notional {notional:.2f} < MIN_ORDER_NOTIONAL {MIN_ORDER_NOTIONAL:.2f}"
                print(f"⚠️ {symbol} {note}")
                logs.append([now_iso_utc(), "BUY-SKIP", symbol, f"{notional:.2f}", "", "", "SKIPPED", note])
                continue

            order = place_buy_notional(api, symbol, notional, EXTENDED_HOURS)
            qty = getattr(order, "qty", "") or ""  # may be empty for notional before fill
            status = getattr(order, "status", "submitted")
            oid = getattr(order, "id", "")

            print(f"✅ Submitted BUY {symbol} ${notional:.2f} (order {oid}, status {status})")

            logs.append([now_iso_utc(), "BUY", symbol, f"{notional:.2f}", str(qty), oid, status, ""])
            time.sleep(SLEEP_BETWEEN_ORDERS_SEC)

        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            print(f"❌ {symbol} {msg}")
            logs.append([now_iso_utc(), "BUY-ERROR", symbol, "", "", "", "ERROR", msg])

    # Write logs
    append_logs(log_ws, logs)
    print("✅ Buy cycle complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print("❌ Fatal error:", e)
        traceback.print_exc()
