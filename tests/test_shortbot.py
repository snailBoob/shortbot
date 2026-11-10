from contextlib import closing
from decimal import Decimal
from email.header import decode_header, make_header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import shortbot
import exchanges


def ticker(price="2.01", volume="10000000", symbol="AKEUSDT", opening="1"):
    return {"symbol": symbol, "lastPrice": price, "open": opening, "quoteVol": volume}


class SignalsTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(shortbot.select_alerts([ticker(price="2")]), [])
        self.assertEqual(shortbot.select_alerts([ticker(volume="9999999.99")]), [])
        alert, = shortbot.select_alerts([ticker()])
        self.assertEqual(alert.increase, Decimal("101"))
        self.assertEqual(alert.volume, Decimal("10000000"))

    def test_invalid_values_do_not_hide_valid_coins(self):
        with self.assertLogs("shortbot", level="WARNING"):
            alerts = shortbot.select_alerts([
                ticker(opening="0"), ticker(price="NaN"), ticker(volume="Infinity"),
                ticker(price="-2"), {}, None, ticker(symbol="BTCUSD"), ticker(),
            ])
        self.assertEqual([a.symbol for a in alerts], ["AKEUSDT"])

    def test_notification(self):
        alert, = shortbot.select_alerts([ticker(symbol="BIRBUSDT")])
        self.assertEqual(alert.url, "https://www.tradingview.com/chart/?symbol=BITUNIX%3ABIRBUSDT.P")
        self.assertEqual(alert.message,
                         f"Ticker: [BITUNIX:BIRBUSDT.P]({alert.url})\n"
                         "Increase: 101.00%\nVolume: 10,000,000.00")

    def test_hour_boundaries(self):
        self.assertEqual(shortbot.next_hour(3599.999), 3600)
        self.assertEqual(shortbot.next_hour(3600), 7200)
        self.assertEqual(shortbot.next_hour(3601), 7200)


class CooldownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "state.sqlite3")
        self.db = shortbot.open_state(self.path)
        self.addCleanup(self.db.close)
        self.send = Mock()
        self.high = Mock(return_value=Decimal("2.01"))
        self.now = 1_000_000

    def scan(self, rows=None):
        return shortbot.scan(self.db, "https://ntfy.test", "Shortbot",
                             fetch=lambda: rows if rows is not None else [ticker()],
                             send=self.send, clock=lambda: self.now, fetch_high=self.high)

    def test_restart_and_exact_24_hour_boundary(self):
        self.assertEqual(self.scan(), 1)
        self.db.close()
        self.db = shortbot.open_state(self.path)
        self.addCleanup(self.db.close)
        self.now += 86399
        self.assertEqual(self.scan(), 0)
        self.now += 1
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.send.call_count, 2)

    def test_disappearing_coin_does_not_reset_cooldown(self):
        self.scan()
        self.now += 3600
        self.scan([ticker(price="1")])
        self.now += 3600
        self.assertEqual(self.scan(), 0)
        self.assertEqual(self.send.call_count, 1)

    def test_each_coin_independent_and_duplicate_rows_suppressed(self):
        self.assertEqual(self.scan([ticker(), ticker(), ticker(symbol="BIRBUSDT")]), 2)

    def test_timeout_reservation_survives_restart(self):
        self.send.side_effect = TimeoutError("ambiguous delivery")
        with self.assertLogs("shortbot", level="ERROR"):
            self.assertEqual(self.scan(), 0)
        self.db.close()
        self.db = shortbot.open_state(self.path)
        self.addCleanup(self.db.close)
        self.send.side_effect = None
        self.now += 3600
        self.assertEqual(self.scan(), 0)
        self.assertEqual(self.send.call_count, 1)

    def test_definitive_rejection_can_retry(self):
        self.send.side_effect = HTTPError("https://ntfy.test", 429, "rate limited", {}, None)
        with self.assertLogs("shortbot", level="ERROR"):
            self.scan()
        self.send.side_effect = None
        self.now += 3600
        self.assertEqual(self.scan(), 1)

    def test_market_failure_leaves_state_unchanged(self):
        with self.assertRaises(ValueError):
            shortbot.scan(self.db, "", "", fetch=Mock(side_effect=ValueError("API failure")))
        self.assertEqual(self.db.execute("SELECT count(*) FROM alerts").fetchone()[0], 0)

    def test_hourly_high_changes_message_not_selection_or_cooldown(self):
        self.high.return_value = Decimal("2.29")
        self.assertEqual(self.scan([ticker(price="2.0235")]), 1)
        alert = self.send.call_args.args[0]
        self.assertEqual(alert.increase, Decimal("129"))
        self.assertIn("Increase: 129.00%", alert.message)
        self.assertEqual(alert.volume, Decimal("10000000"))
        self.high.assert_called_once_with("bitunix", "AKEUSDT", "", self.now // 3600 * 3600)
        self.assertEqual(self.scan(), 0)
        self.assertEqual(self.high.call_count, 1)

    def test_no_candle_requests_for_rejected_candidates(self):
        self.assertEqual(self.scan([ticker(price="2"), ticker(volume="9999999")]), 0)
        self.high.assert_not_called()
        self.send.assert_not_called()

    def test_hourly_high_does_not_reapply_signal_threshold(self):
        self.high.return_value = Decimal("1.99")
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.send.call_args.args[0].increase, Decimal("99"))

    def test_candle_failure_falls_back_and_keeps_cooldown(self):
        for failure in (ValueError("Missing candle"), TimeoutError("Timed out"),
                        HTTPError("https://market.test", 429, "Rate limited", {}, None)):
            with self.subTest(failure=failure):
                self.db.execute("DELETE FROM alerts")
                self.db.commit()
                self.high.reset_mock()
                self.high.side_effect = failure
                with self.assertLogs("shortbot", level="WARNING") as logs:
                    self.assertEqual(self.scan(), 1)
                self.assertIn("using ticker increase 101.00%", logs.output[0])
                self.assertIn("BITUNIX:AKEUSDT.P", logs.output[0])
                self.assertEqual(self.send.call_args.args[0].increase, Decimal("101"))
                self.assertEqual(self.scan(), 0)
                self.assertEqual(self.high.call_count, 1)
                self.assertEqual(self.db.execute("SELECT status FROM alerts").fetchone()[0], "sent")

    def test_candle_failure_does_not_stop_other_alerts(self):
        self.high.side_effect = [ValueError("Missing candle"), Decimal("2.29")]
        with self.assertLogs("shortbot", level="WARNING"):
            self.assertEqual(self.scan([ticker(), ticker(symbol="BIRBUSDT")]), 2)
        self.assertEqual([call.args[0].increase for call in self.send.call_args_list],
                         [Decimal("101"), Decimal("129")])

    def test_scan_freezes_candle_hour_before_slow_market_request(self):
        clock = Mock(side_effect=[7199, 7201, 7202])
        shortbot.scan(self.db, "", "", fetch=lambda: [ticker()], send=self.send,
                      clock=clock, fetch_high=self.high)
        self.high.assert_called_once_with("bitunix", "AKEUSDT", "", 3600)


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.market = {"code": 0, "data": [ticker()]}
        self.ack = {"id": "test-id", "event": "message"}
        self.required_token = None
        self.candle_requests = []
        self.candles = [{"time": 3_600_000, "high": "2.29"}]
        self.env = patch.dict(os.environ, {"NTFY_TOKEN": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.endswith("trading_pairs"):
                    self.respond({"code": 0, "data": [{"symbol": "AKEUSDT", "base": "AKE", "quote": "USDT", "symbolStatus": "OPEN"}]})
                elif self.path.startswith("/kline?"):
                    owner.candle_requests.append(self.path)
                    self.respond({"code": 0, "data": owner.candles})
                else:
                    self.respond(owner.market)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if owner.required_token and self.headers.get("Authorization") != f"Bearer {owner.required_token}":
                    self.send_error(403)
                    return
                owner.posts.append((self.path, self.headers, body.decode("utf-8")))
                self.respond(owner.ack)

            def respond(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def fetch(self):
        return exchanges.fetch_markets("bitunix", lambda url: exchanges.request_json(self.url + "/" + url.rsplit("/", 1)[-1]))

    def fetch_high(self, exchange, symbol, market_type, hour_end):
        return exchanges.fetch_hour_high(exchange, symbol, market_type, hour_end,
            get=lambda url: exchanges.request_json(self.url + "/" + url.rsplit("/", 1)[-1]))

    def test_end_to_end_http_and_persistent_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            with closing(shortbot.open_state(str(Path(tmp) / "state.db"))) as db:
                for _ in range(2):
                    shortbot.scan(db, self.url, "Shortbot",
                                  fetch=self.fetch, fetch_high=self.fetch_high, clock=lambda: 7200)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.candle_requests), 1)
        path, headers, body = self.posts[0]
        self.assertEqual(path, "/Shortbot")
        self.assertEqual(headers["Markdown"], "yes")
        self.assertEqual(str(make_header(decode_header(headers["Title"]))), "💩 Shortbot")
        self.assertIsNone(headers["Click"])
        self.assertIsNone(headers["Actions"])
        self.assertIsNone(headers["Authorization"])
        self.assertIn("[BITUNIX:AKEUSDT.P](https://www.tradingview.com/chart/?symbol=BITUNIX%3AAKEUSDT.P)", body)
        self.assertIn("Increase: 129.00%", body)
        self.assertIn("Volume: 10,000,000.00", body)
        self.assertFalse(body.endswith(" USDT"))

    def test_missing_candle_still_delivers_original_alert_over_http(self):
        self.candles = []
        with closing(shortbot.open_state(":memory:")) as db:
            with self.assertLogs("shortbot", level="WARNING"):
                self.assertEqual(shortbot.scan(db, self.url, "Shortbot", fetch=self.fetch,
                    fetch_high=self.fetch_high, clock=lambda: 7200), 1)
        self.assertIn("Increase: 101.00%", self.posts[0][2])

    def test_unsuccessful_api_response(self):
        for market in ({"code": 1, "data": []}, {"code": 0, "data": []}, {"code": 0, "data": {}}):
            self.market = market
            with self.assertRaises(ValueError):
                self.fetch()

    def test_authenticated_publish_and_rejection(self):
        self.required_token = "test-token"
        alert, = shortbot.select_alerts([ticker()])
        for token in ("", "wrong-token"):
            with self.subTest(token=token), patch.dict(os.environ, {"NTFY_TOKEN": token}):
                with self.assertRaises(HTTPError) as caught:
                    shortbot.publish(alert, self.url, "Shortbot")
                self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.posts, [])
        with patch.dict(os.environ, {"NTFY_TOKEN": self.required_token}):
            shortbot.publish(alert, self.url, "Shortbot")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][1]["Authorization"], "Bearer test-token")
        self.assertNotIn("test-token", self.posts[0][2])

    def test_invalid_ntfy_acknowledgment(self):
        self.ack = {"error": "not accepted"}
        with self.assertRaises(ValueError):
            shortbot.publish(shortbot.select_alerts([ticker()])[0], self.url, "Shortbot")


class DryRunTest(unittest.TestCase):
    def test_dry_run_uses_enrichment_without_state_or_publication(self):
        # Exercise the actual main path while all exchange requests stay in-process.
        with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["shortbot.py", "--dry-run"]), \
             patch.object(shortbot, "fetch_markets", return_value=[ticker()]), \
             patch.object(shortbot, "open_state") as state, patch.object(shortbot, "publish") as send, \
             patch("sys.stdout", new_callable=io.StringIO) as output, \
             patch.object(shortbot.time, "time", return_value=7200), \
             patch.object(shortbot.logging, "basicConfig"):
            # fetch_hour_high's default transport is bound at definition time.
            with patch("exchanges.urlopen") as opened:
                opened.return_value.__enter__.return_value.read.return_value = json.dumps({
                    "code": 0, "data": [{"time": 3_600_000, "high": "2.29"}]}).encode()
                shortbot.main()
            self.assertIn("Increase: 129.00%", output.getvalue())
            state.assert_not_called()
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
