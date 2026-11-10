from datetime import datetime
from decimal import Decimal
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import exchanges
import shortbot


class AdapterTest(unittest.TestCase):
    def test_bitunix_filters_closed_markets_and_preserves_quote(self):
        def get(url):
            if url.endswith("trading_pairs"):
                return {"code": 0, "data": [
                    {"symbol": "ETHBTC", "base": "ETH", "quote": "BTC", "symbolStatus": "OPEN"},
                    {"symbol": "OLDUSDT", "symbolStatus": "CLOSED"}]}
            return {"code": 0, "data": [
                {"symbol": "ETHBTC", "open": "0.01", "lastPrice": "0.03", "quoteVol": "200"},
                {"symbol": "OLDUSDT"}]}
        result, = exchanges.fetch_markets("bitunix", get)
        self.assertEqual(result["quote"], "BTC")
        self.assertEqual(result["quoteVol"], "200")
        self.assertEqual(result["market_symbol"], "ETHBTC")

    def test_binance_inverse_contracts_to_usd_and_excludes_expiry(self):
        def get(url):
            inverse = "dapi" in url
            symbol = "BTCUSD_PERP" if inverse else "ETHUSDT"
            if url.endswith("exchangeInfo"):
                return {"symbols": [
                    {"symbol": symbol, "contractType": "PERPETUAL", "status": "TRADING",
                     "baseAsset": "BTC" if inverse else "ETH", "quoteAsset": "USD" if inverse else "USDT", "contractSize": 100},
                    {"symbol": "EXPIRY", "contractType": "CURRENT_QUARTER"}]}
            return [{"symbol": symbol, "openPrice": "1", "lastPrice": "3", "volume": "150000", "quoteVolume": "12345678"},
                    {"symbol": "EXPIRY"}]
        rows = exchanges.fetch_markets("binance", get)
        self.assertEqual([r["symbol"] for r in rows], ["ETHUSDT", "BTCUSD"])
        self.assertEqual(Decimal(rows[1]["quoteVol"]), 15_000_000)
        self.assertEqual(Decimal(rows[0]["quoteVol"]), 12_345_678)
        self.assertEqual([(r["market_symbol"], r["market_type"]) for r in rows],
                         [("ETHUSDT", "fapi"), ("BTCUSD_PERP", "dapi")])

    def test_bybit_pagination_usdc_symbols_and_inverse_quote_volume(self):
        calls = []
        def get(url):
            calls.append(url)
            inverse = "category=inverse" in url
            if "instruments-info" in url:
                if not inverse and "cursor=next" not in url:
                    return {"retCode": 0, "result": {"list": [], "nextPageCursor": "next"}}
                return {"retCode": 0, "result": {"list": [
                    {"symbol": "BTCUSD" if inverse else "ETHPERP", "baseCoin": "BTC" if inverse else "ETH",
                     "quoteCoin": "USD" if inverse else "USDC", "status": "Trading",
                     "contractType": "InversePerpetual" if inverse else "LinearPerpetual"},
                    {"contractType": "LinearFutures"}]}}
            return {"retCode": 0, "result": {"list": [
                {"symbol": "BTCUSD" if inverse else "ETHPERP", "prevPrice24h": "1", "lastPrice": "3",
                 "volume24h": "12000000", "turnover24h": "15000000"}]}}
        rows = exchanges.fetch_markets("bybit", get)
        self.assertTrue(any("cursor=next" in call for call in calls))
        self.assertEqual([r["symbol"] for r in rows], ["ETHUSDC", "BTCUSD"])
        self.assertEqual(rows[1]["quoteVol"], "12000000")
        self.assertEqual(rows[0]["quoteVol"], "15000000")
        alert, = shortbot.select_alerts(rows, exchange="bybit", quotes=("USDC",))
        self.assertEqual((alert.market_symbol, alert.market_type), ("ETHPERP", "linear"))
        self.assertEqual(alert.reference_price, Decimal("1"))
        self.assertEqual(rows[1]["market_type"], "inverse")

    def test_hyperliquid_mark_price_and_delisted_filter(self):
        def get(url, body):
            self.assertEqual(body, {"type": "metaAndAssetCtxs"})
            return [{"universe": [{"name": "kPEPE"}, {"name": "OLD", "isDelisted": True}]},
                    [{"prevDayPx": "1", "markPx": "3", "dayNtlVlm": "12000000"}, {}]]
        result, = exchanges.fetch_markets("hyperliquid", get)
        self.assertEqual(result["symbol"], "KPEPEUSDC")
        self.assertEqual(result["lastPrice"], "3")
        self.assertEqual(result["market_symbol"], "kPEPE")

    def test_api_failures_are_not_silent_empty_success(self):
        for exchange in exchanges.EXCHANGES:
            with self.subTest(exchange=exchange), self.assertRaises(ValueError):
                exchanges.fetch_markets(exchange, lambda *args: {})
        with self.assertRaises(ValueError):
            exchanges.fetch_markets("lbank")


class HourHighTest(unittest.TestCase):
    # The requested closed hour is 01:00-02:00 UTC on the Unix epoch date.
    start = 3_600_000
    end = 7_200_000

    def test_bitunix_closed_hour_and_trade_price_request(self):
        get = Mock(return_value={"code": 0, "data": [{"time": self.start, "high": "2.29"}]})
        self.assertEqual(exchanges.fetch_hour_high("bitunix", "INITUSDT", "", 7200, get), Decimal("2.29"))
        url = urlsplit(get.call_args.args[0])
        self.assertEqual(url.path, "/api/v1/futures/market/kline")
        self.assertEqual(parse_qs(url.query), {"symbol": ["INITUSDT"], "interval": ["1h"],
            "endTime": [str(self.end)], "limit": ["1"], "type": ["LAST_PRICE"]})

    def test_binance_usdm_and_coinm_contract_routing(self):
        for market, symbol in (("fapi", "INITUSDT"), ("dapi", "BTCUSD_PERP")):
            with self.subTest(market=market):
                get = Mock(return_value=[[self.start, "1", "2.29", "1", "2.02", "10", self.end - 1]])
                self.assertEqual(exchanges.fetch_hour_high("binance", symbol, market, 7200, get), Decimal("2.29"))
                url = urlsplit(get.call_args.args[0])
                self.assertEqual(url.netloc, market + ".binance.com")
                self.assertEqual(url.path, f"/{market}/v1/klines")
                self.assertEqual(parse_qs(url.query), {"symbol": [symbol], "interval": ["1h"],
                    "startTime": [str(self.start)], "endTime": [str(self.end - 1)], "limit": ["1"]})

    def test_bybit_linear_usdc_and_inverse_contract_routing(self):
        for category, symbol in (("linear", "ETHPERP"), ("inverse", "BTCUSD")):
            with self.subTest(category=category):
                get = Mock(return_value={"retCode": 0, "result": {"list": [[str(self.start), "1", "2.29"]]}})
                self.assertEqual(exchanges.fetch_hour_high("bybit", symbol, category, 7200, get), Decimal("2.29"))
                url = urlsplit(get.call_args.args[0])
                self.assertEqual(url.path, "/v5/market/kline")
                self.assertEqual(parse_qs(url.query), {"category": [category], "symbol": [symbol],
                    "interval": ["60"], "start": [str(self.start)], "end": [str(self.end - 1)], "limit": ["1"]})

    def test_hyperliquid_preserves_native_coin_case(self):
        get = Mock(return_value=[{"t": self.start, "T": self.end - 1, "s": "kPEPE", "i": "1h", "h": "2.29"}])
        self.assertEqual(exchanges.fetch_hour_high("hyperliquid", "kPEPE", "", 7200, get), Decimal("2.29"))
        get.assert_called_once_with("https://api.hyperliquid.xyz/info", {"type": "candleSnapshot", "req": {
            "coin": "kPEPE", "interval": "1h", "startTime": self.start, "endTime": self.end - 1}})

    def test_missing_stale_forming_malformed_and_invalid_candles_are_rejected(self):
        for exchange, symbol, market in (("bitunix", "BTCUSDT", ""), ("binance", "BTCUSDT", "fapi"),
                                        ("bybit", "BTCUSDT", "linear"), ("hyperliquid", "BTC", "")):
            def payload(timestamp, high):
                if exchange == "bitunix":
                    return {"code": 0, "data": [{"time": timestamp, "high": high}]}
                if exchange == "binance":
                    return [[timestamp, "1", high, "1", "2", "10", timestamp + 3_600_000 - 1]]
                if exchange == "bybit":
                    return {"retCode": 0, "result": {"list": [[str(timestamp), "1", high]]}}
                return [{"t": timestamp, "T": timestamp + 3_600_000 - 1, "s": symbol, "i": "1h", "h": high}]
            responses = [{}, [], payload(0, "2"), payload(self.end, "2")]
            responses += [payload(self.start, v) for v in (None, "NaN", "Infinity", "0", "-1", "oops")]
            for response in responses:
                with self.subTest(exchange=exchange, response=response), self.assertRaises(ValueError):
                    exchanges.fetch_hour_high(exchange, symbol, market, 7200, Mock(return_value=response))

    def test_exchange_error_and_wrong_hyperliquid_identity_are_rejected(self):
        cases = [("bitunix", "", {"code": 1, "data": [{"time": self.start, "high": "2"}]}),
                 ("bybit", "linear", {"retCode": 1, "result": {"list": [[self.start, "1", "2"]]}}),
                 ("hyperliquid", "", [{"t": self.start, "T": self.end - 1, "s": "WRONG", "i": "1h", "h": "2"}])]
        for exchange, market, response in cases:
            with self.subTest(exchange=exchange), self.assertRaises(ValueError):
                exchanges.fetch_hour_high(exchange, "BTC", market, 7200, Mock(return_value=response))

class SelectionTest(unittest.TestCase):
    def test_btc_is_quote_not_base_and_volume_converted_before_threshold(self):
        rows = [exchanges.row("BTCUSDT", "BTC", "USDT", "20000", "50000", "100000000"),
                exchanges.row("ETHBTC", "ETH", "BTC", "0.01", "0.03", "200")]
        alert, = shortbot.select_alerts(rows, quotes=("BTC",))
        self.assertEqual(alert.symbol, "ETHBTC")
        self.assertEqual(alert.volume, Decimal("10000000"))
        self.assertEqual(alert.cooldown_key, "ETHUSDT")
        rows[1]["quoteVol"] = "199.999"
        self.assertEqual(shortbot.select_alerts(rows, quotes=("BTC",)), [])

    def test_missing_conversion_does_not_use_btc_as_usd(self):
        with self.assertLogs("shortbot", level="WARNING"):
            self.assertEqual(shortbot.select_alerts([
                exchanges.row("ETHBTC", "ETH", "BTC", "1", "3", "10000000")]), [])

    def test_configurable_thresholds_and_link(self):
        rows = [exchanges.row("ETHUSDC", "ETH", "USDC", "100", "150", "500000")]
        self.assertEqual(shortbot.select_alerts(rows, increase_threshold=Decimal("50")), [])
        alert, = shortbot.select_alerts(rows, exchange="hyperliquid", increase_threshold=Decimal("49"),
                                       volume_threshold=Decimal("500000"))
        self.assertEqual(alert.url, "https://www.tradingview.com/chart/?symbol=HYPERLIQUID%3AETHUSDC.P")
        self.assertEqual(alert.cooldown_key, "ETHUSDT")

    def test_configuration_defaults_overrides_and_rejection(self):
        with patch.dict(os.environ, {}, clear=True):
            config, timezone = shortbot.configuration()
        self.assertEqual(config["increase_threshold"], 100)
        self.assertEqual(config["volume_threshold"], 10_000_000)
        self.assertEqual(timezone.key, "UTC")
        with patch.dict(os.environ, {"EXCHANGE": "BYBIT", "QUOTE_CURRENCIES": "BTC, USDC", "INCREASE_THRESHOLD": "75.5"}, clear=True):
            config, _ = shortbot.configuration()
        self.assertEqual(config["quotes"], ("BTC", "USDC"))
        self.assertEqual(config["increase_threshold"], Decimal("75.5"))
        for values in ({"EXCHANGE": "lbank"}, {"VOLUME_THRESHOLD": "NaN"}, {"INCREASE_THRESHOLD": "-1"}, {"QUOTE_CURRENCIES": "EUR"}):
            with patch.dict(os.environ, values, clear=True), self.assertRaises(ValueError):
                shortbot.configuration()

    def test_timezone_half_hour_and_dst(self):
        now = datetime.fromisoformat("2026-01-01T12:10:00+00:00").timestamp()
        target = shortbot.next_hour(now, ZoneInfo("Asia/Kolkata"))
        self.assertEqual(datetime.fromtimestamp(target, ZoneInfo("Asia/Kolkata")).isoformat(), "2026-01-01T18:00:00+05:30")
        now = datetime.fromisoformat("2026-10-25T00:30:00+00:00").timestamp()
        target = shortbot.next_hour(now, ZoneInfo("Europe/Amsterdam"))
        self.assertEqual(datetime.fromtimestamp(target, ZoneInfo("Europe/Amsterdam")).isoformat(), "2026-10-25T02:00:00+01:00")
