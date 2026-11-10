"""Hourly perpetual signals delivered through ntfy. Python standard library only."""

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.header import Header
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from exchanges import EXCHANGES, USD_QUOTES, fetch_markets


LOG = logging.getLogger("shortbot")
COOLDOWN = 24 * 60 * 60
STOP = threading.Event()


@dataclass(frozen=True)
class Alert:
    symbol: str
    increase: Decimal
    volume: Decimal
    exchange: str = "bitunix"
    base: str = ""

    @property
    def ticker(self):
        return f"{self.exchange.upper()}:{self.symbol}.P"

    @property
    def cooldown_key(self):
        # Preserve existing Bitunix cooldown keys; share cooldown across quotes/exchanges.
        return self.base + "USDT" if self.base else self.symbol

    @property
    def url(self):
        return "https://www.tradingview.com/chart/?symbol=" + quote(self.ticker, safe="")

    @property
    def message(self):
        return (
            f"Ticker: [{self.ticker}]({self.url})\n"
            f"Increase: {self.increase:.2f}%\n"
            f"Volume: {self.volume:,.2f}"
        )


def select_alerts(rows, exchange="bitunix", quotes=("USDT", "USD", "USDC", "BTC"),
                  increase_threshold=Decimal("100"), volume_threshold=Decimal("10000000")):
    alerts = []
    btc_usd = None
    for row in rows:
        if isinstance(row, dict) and row.get("base") == "BTC" and row.get("quote") in USD_QUOTES:
            try:
                value = Decimal(row["lastPrice"])
                if value.is_finite() and value > 0:
                    btc_usd = value
                    break
            except (KeyError, InvalidOperation, TypeError):
                continue
    for row in rows:
        try:
            symbol = row["symbol"]
            currency = row.get("quote", "USDT")
            if currency not in quotes:
                continue
            if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+" + re.escape(currency), symbol):
                continue
            opening = Decimal(row["open"])
            price = Decimal(row["lastPrice"])
            volume = Decimal(row["quoteVol"])
            if currency == "BTC":
                if btc_usd is None:
                    raise ValueError("BTC/USD conversion price unavailable")
                volume *= btc_usd
            if not all(value.is_finite() for value in (opening, price, volume)):
                raise ValueError("Non-finite ticker value")
            if opening <= 0 or price <= 0 or volume < 0:
                raise ValueError("Invalid ticker value")
            increase = (price / opening - 1) * 100
            if increase > increase_threshold and volume >= volume_threshold:
                alerts.append(Alert(symbol, increase, volume, exchange, row.get("base", symbol[:-len(currency)])))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            LOG.warning("Skipping invalid ticker: %r", row)
    return sorted(alerts, key=lambda alert: alert.symbol)


def publish(alert, server, topic):
    request = Request(
        f"{server.rstrip('/')}/{quote(topic, safe='')}",
        data=alert.message.encode("utf-8"),
        headers={
            "Content-Type": "text/markdown; charset=utf-8",
            "Markdown": "yes",
            "Title": Header("💩 Shortbot", "utf-8").encode(),
        },
        method="POST",
    )
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("event") != "message" or not payload.get("id"):
        raise ValueError("ntfy did not acknowledge the notification")


def open_state(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS alerts "
        "(symbol TEXT PRIMARY KEY, reserved_at REAL NOT NULL, status TEXT NOT NULL)"
    )
    db.commit()
    return db


def reserve(db, symbol, now):
    # Persist BEFORE the HTTP request: a timeout or restart must not duplicate an alert.
    with db:
        cursor = db.execute(
            "INSERT INTO alerts VALUES (?, ?, 'pending') "
            "ON CONFLICT(symbol) DO UPDATE SET reserved_at=excluded.reserved_at, status='pending' "
            "WHERE alerts.reserved_at <= ?",
            (symbol, now, now - COOLDOWN),
        )
    return cursor.rowcount == 1


def scan(db, server, topic, *, fetch=None, send=publish, clock=time.time, **selection):
    if fetch is None:
        fetch = lambda: fetch_markets(selection.get("exchange", "bitunix"))
    alerts = select_alerts(fetch(), **selection)
    sent = 0
    for alert in alerts:
        if STOP.is_set():
            break
        if not reserve(db, alert.cooldown_key, clock()):
            continue
        try:
            send(alert, server, topic)
        except HTTPError as error:
            # A definitive client-side rejection cannot have delivered the notification.
            if 400 <= error.code < 500 and error.code != 408:
                with db:
                    db.execute("DELETE FROM alerts WHERE symbol=?", (alert.cooldown_key,))
                LOG.exception("ntfy rejected %s; retry at the next hourly scan", alert.symbol)
            else:
                LOG.exception("Delivery uncertain for %s; retaining 24h cooldown", alert.symbol)
        except Exception:
            LOG.exception("Delivery uncertain for %s; retaining 24h cooldown", alert.symbol)
        else:
            with db:
                db.execute("UPDATE alerts SET status='sent', reserved_at=? WHERE symbol=?",
                           (clock(), alert.cooldown_key))
            sent += 1
            LOG.info("Sent %s: increase=%.2f%%, volume=%s USD-equivalent", alert.ticker, alert.increase, alert.volume)
    LOG.info("Scan complete: %d qualifying coins, %d notifications sent", len(alerts), sent)
    return sent


def next_hour(now, timezone=ZoneInfo("UTC")):
    # Search in absolute time, including half-hour zones and repeated DST hours.
    candidate = (int(now) // 60 + 1) * 60
    while datetime.fromtimestamp(candidate, timezone).minute != 0:
        candidate += 60
    return candidate


def configuration():
    exchange = os.environ.get("EXCHANGE", "bitunix").strip().lower()
    if exchange not in EXCHANGES:
        raise ValueError(f"EXCHANGE must be one of {', '.join(EXCHANGES)}")
    quotes = tuple(dict.fromkeys(q.strip().upper() for q in os.environ.get(
        "QUOTE_CURRENCIES", "USDT,USD,USDC,BTC").split(",")))
    if not quotes or any(q not in USD_QUOTES | {"BTC"} for q in quotes):
        raise ValueError("QUOTE_CURRENCIES must contain supported USD currencies or BTC")
    thresholds = {}
    for variable, default in (("INCREASE_THRESHOLD", "100"), ("VOLUME_THRESHOLD", "10000000")):
        value = Decimal(os.environ.get(variable, default))
        if not value.is_finite() or value < 0:
            raise ValueError(f"{variable} must be finite and non-negative")
        thresholds[variable.lower()] = value
    return {"exchange": exchange, "quotes": quotes, **thresholds}, ZoneInfo(os.environ.get("TZ", "UTC"))


class ZonedFormatter(logging.Formatter):
    def __init__(self, timezone):
        super().__init__("%(asctime)s %(levelname)s %(message)s")
        self.timezone = timezone

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, self.timezone).isoformat(timespec="seconds")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print matches, without sending or saving state")
    args = parser.parse_args()
    selection, timezone = configuration()
    handler = logging.StreamHandler()
    handler.setFormatter(ZonedFormatter(timezone))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    fetch = lambda: fetch_markets(selection["exchange"])
    if args.dry_run:
        rows = fetch()
        alerts = select_alerts(rows, **selection)
        for alert in alerts:
            print(alert.message, end="\n\n")
        LOG.info("Dry run %s: %d active perpetuals, %d qualifying pairs", selection["exchange"], len(rows), len(alerts))
        return

    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    topic = os.environ.get("NTFY_TOPIC", "")
    if not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic):
        raise ValueError("Set NTFY_TOPIC to your own topic (1-64 letters, digits, hyphens or underscores)")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())

    with closing(open_state(os.environ.get("STATE_PATH", "data/shortbot.sqlite3"))) as db:
        target = next_hour(time.time(), timezone)
        LOG.info("Shortbot started: %s, quotes=%s; first scan at %s", selection["exchange"],
                 selection["quotes"], datetime.fromtimestamp(target, timezone).isoformat())
        while not STOP.is_set():
            remaining = target - time.time()
            if remaining > 0:
                STOP.wait(min(remaining, 1))
                continue
            # Skip old scans after suspension instead of issuing stale notifications.
            if time.time() - target < 60:
                try:
                    scan(db, server, topic, fetch=fetch, **selection)
                except (OSError, ValueError) as error:
                    LOG.error("Hourly scan failed: %s; retry at the next hour", error)
            target = next_hour(time.time(), timezone)
    LOG.info("Shortbot stopped")


if __name__ == "__main__":
    main()
