FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System deps (ssl/certs, locales, etc.); add curl if you like debugging
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install -r requirements.txt

COPY main.py /app/

# Required envs at runtime:
# - ALPACA_API_KEY
# - ALPACA_SECRET_KEY
# - APCA_API_BASE_URL   (e.g., https://api.alpaca.markets or https://paper-api.alpaca.markets)
# - GOOGLE_CREDS_JSON   (service account JSON, single-line or raw JSON)
# Optional:
# - SHEET_NAME (default "Trading Log"), SCREENER_TAB (default "screener"), LOG_TAB (default "log")
# - PERCENT_PER_TRADE (default 5.0), MIN_ORDER_NOTIONAL (default 1.00)
# - SLEEP_BETWEEN_ORDERS_SEC (default 0.5), EXTENDED_HOURS (default false)

CMD ["python", "/app/main.py"]
