from datetime import datetime
from decimal import Decimal
import os
import unittest
from unittest.mock import patch
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

    def test_hyperliquid_mark_price_and_delisted_filter(self):
        def get(url, body):
            self.assertEqual(body, {"type": "metaAndAssetCtxs"})
            return [{"universe": [{"name": "kPEPE"}, {"name": "OLD", "isDelisted": True}]},
                    [{"prevDayPx": "1", "markPx": "3", "dayNtlVlm": "12000000"}, {}]]
        result, = exchanges.fetch_markets("hyperliquid", get)
        self.assertEqual(result["symbol"], "KPEPEUSDC")
        self.assertEqual(result["lastPrice"], "3")

    def test_api_failures_are_not_silent_empty_success(self):
        for exchange in exchanges.EXCHANGES:
            with self.subTest(exchange=exchange), self.assertRaises(ValueError):
                exchanges.fetch_markets(exchange, lambda *args: {})
        with self.assertRaises(ValueError):
            exchanges.fetch_markets("lbank")


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
