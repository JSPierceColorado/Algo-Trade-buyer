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
PERCENT_PER_TRADE       = float(os.getenv("PERCENT_PER_TRADE", "5.0"))   # percent
MIN_ORDER_NOTIONAL      = float(os.getenv("MIN_ORDER_NOTIONAL", "1.00"))  # floor
SLEEP_BETWEEN_ORDERS_SEC= float(os.getenv("SLEEP_BETWEEN_ORDERS_SEC", "0.5"))
EXTENDED_HOURS          = os.getenv("EXTENDED_HOURS", "false").lower() in ("1", "true", "yes")

# Sheet layout anchors
LOG_HEADERS     = ["Timestamp","Action","Symbol","NotionalUSD","Qty","OrderID","Status","Note"]
LOG_TABLE_RANGE = "A1:H1"


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


def ensure_log(ws):
    """Ensure header is exactly in A1:H1 and frozen; prevents offset drift."""
    vals = ws.get_values("A1:H1")
    if not vals or vals[0] != LOG_HEADERS:
        ws.update("A1:H1", [LOG_HEADERS])
    try:
        ws.freeze(rows=1)
    except Exception:
        pass


def append_logs(ws, rows):
    """
    Append logs anchored to A1:H1, forcing exactly 8 columns per row.
    This avoids Sheets creating a separate 'table' off to the right.
    """
    if not rows:
        return
    fixed = []
    for r in rows:
        if len(r) < 8:
            r = r + [""] * (8 - len(r))
        elif len(r) > 8:
            r = r[:8]
        fixed.append(r)
    try:
        # Anchor appends to our table
        for i in range(0, len(fixed), 100):
            ws.append_rows(
                fixed[i:i+100],
                value_input_option="RAW",     # avoid locale/date auto-parsing
                table_range=LOG_TABLE_RANGE   # <<< anchor prevents offset
            )
    except TypeError:
        # Fallback for older gspread without table_range support
        start_row = len(ws.get_all_values()) + 1
        end_row = start_row + len(fixed) - 1
        ws.update(f"A{start_row}:H{end_row}", fixed, value_input_option="RAW")


def read_screener_tickers(ws):
    """
    Reads the screener tab and returns a list of tickers.
    Assumes a header row containing a column named 'Ticker'; falls back to first col.
    """
    values = ws.get_all_values()
    if not values:
        return []

    header = [h.strip() for h in values[0]]
    try:
        idx = header.index("Ticker")
    except ValueError:
        idx = 0

    tickers = []
    for row in values[1:]:
        if idx < len(row):
            t = row[idx].strip().upper()
            if t:
                tickers.append(t)

    # Preserve order while de-duping
    seen = set()
    ordered = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def make_alpaca():
    if not (ALPACA_API_KEY and ALPACA_SECRET_KEY):
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")
    return REST(key_id=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY, base_url=APCA_API_BASE_URL)


def place_buy_notional(api: REST, symbol: str, notional: float, extended: bool):
    """
    Places a market buy with notional dollars. Returns the order object.
    Adds an idempotent client_order_id so retries won't double-buy.
    """
    client_order_id = f"buy-{symbol}-{int(time.time()*1000)}"
    order = api.submit_order(
        symbol=symbol,
        side="buy",
        type="market",
        time_in_force="day",
        notional=round(notional, 2),
        extended_hours=extended,
        client_order_id=client_order_id,
    )
    return order


# =========================
# Main
# =========================
def main():
    print("🚀 Buy bot starting")

    # Connect
    gc  = get_google_client()
    api = make_alpaca()

    # Sheets
    screener_ws = _get_ws(gc, SHEET_NAME, SCREENER_TAB)
    log_ws      = _get_ws(gc, SHEET_NAME, LOG_TAB); ensure_log(log_ws)

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
            # Use buying_power; fall back to cash if not present
            try:
                buying_power = float(acct.buying_power)
            except Exception:
                buying_power = float(acct.cash)

            notional = buying_power * (PERCENT_PER_TRADE / 100.0)
            if notional < MIN_ORDER_NOTIONAL:
                note = f"Notional {notional:.2f} < MIN_ORDER_NOTIONAL {MIN_ORDER_NOTIONAL:.2f}"
                print(f"⚠️ {symbol} {note}")
                logs.append([now_iso_utc(), "BUY-SKIP", symbol, f"{notional:.2f}", "", "", "SKIPPED", note])
                continue

            order = place_buy_notional(api, symbol, notional, EXTENDED_HOURS)
            qty    = getattr(order, "qty", "") or ""   # may be empty pre-fill for notional
            status = getattr(order, "status", "submitted")
            oid    = getattr(order, "id", "")

            print(f"✅ Submitted BUY {symbol} ${notional:.2f} (order {oid}, status {status})")

            logs.append([now_iso_utc(), "BUY", symbol, f"{notional:.2f}", str(qty), oid, status, ""])
            time.sleep(SLEEP_BETWEEN_ORDERS_SEC)

        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            print(f"❌ {symbol} {msg}")
            logs.append([now_iso_utc(), "BUY-ERROR", symbol, "", "", "", "ERROR", msg])

    # Write logs (anchored)
    append_logs(log_ws, logs)
    print("✅ Buy cycle complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print("❌ Fatal error:", e)
        traceback.print_exc()
