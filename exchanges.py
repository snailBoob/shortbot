"""Public perpetual market data, normalized at the exchange boundary."""

import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import Request, urlopen

EXCHANGES = ("bitunix", "binance", "bybit", "hyperliquid")
USD_QUOTES = frozenset(("USD", "USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDE", "USD1"))


def request_json(url, body=None):
    request = Request(url, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json", "User-Agent": "Shortbot/1.0"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def row(symbol, base, quote, opening, price, volume, *, market_symbol=None, market_type=""):
    return {"symbol": symbol, "base": base, "quote": quote, "open": opening,
            "lastPrice": price, "quoteVol": volume,
            "market_symbol": market_symbol or symbol, "market_type": market_type}


def bitunix(get):
    root = "https://fapi.bitunix.com/api/v1/futures/market/"
    markets, tickers = get(root + "trading_pairs"), get(root + "tickers")
    if markets.get("code") != 0 or tickers.get("code") != 0:
        raise ValueError("Bitunix API error")
    instruments = {m["symbol"]: m for m in markets["data"] if m["symbolStatus"] == "OPEN"}
    return [row(t["symbol"], instruments[t["symbol"]]["base"], instruments[t["symbol"]]["quote"],
                t["open"], t["lastPrice"], t["quoteVol"])
            for t in tickers["data"] if t["symbol"] in instruments]


def binance(get):
    rows = []
    for host, api, inverse in (("fapi", "fapi", False), ("dapi", "dapi", True)):
        root = f"https://{host}.binance.com/{api}/v1/"
        markets = get(root + "exchangeInfo")["symbols"]
        instruments = {m["symbol"]: m for m in markets
                       if m["contractType"] == "PERPETUAL"
                       and m.get("status", m.get("contractStatus")) == "TRADING"}
        tickers = get(root + "ticker/24hr")
        if not isinstance(tickers, list):
            raise ValueError("Binance API error")
        for t in tickers:
            m = instruments.get(t["symbol"])
            if m is None:
                continue
            # COIN-M volume is contracts, each with a fixed quote-currency notional.
            volume = str(Decimal(t["volume"]) * Decimal(str(m["contractSize"]))) if inverse else t["quoteVolume"]
            rows.append(row(m["baseAsset"] + m["quoteAsset"], m["baseAsset"], m["quoteAsset"],
                            t["openPrice"], t["lastPrice"], volume,
                            market_symbol=t["symbol"], market_type=api))
    return rows


def bybit(get):
    root = "https://api.bybit.com/v5/market/"
    rows = []
    for category in ("linear", "inverse"):
        instruments = {}
        cursor = ""
        seen = set()
        while True:
            params = urlencode({"category": category, "limit": 1000, "cursor": cursor})
            payload = get(root + "instruments-info?" + params)
            if payload.get("retCode") != 0:
                raise ValueError("Bybit instruments API error")
            for m in payload["result"]["list"]:
                if m["contractType"] in ("LinearPerpetual", "InversePerpetual") and m["status"] == "Trading":
                    instruments[m["symbol"]] = m
            cursor = payload["result"].get("nextPageCursor", "")
            if not cursor:
                break
            if cursor in seen:
                raise ValueError("Bybit repeated pagination cursor")
            seen.add(cursor)
        payload = get(root + "tickers?category=" + category)
        if payload.get("retCode") != 0:
            raise ValueError("Bybit tickers API error")
        for t in payload["result"]["list"]:
            m = instruments.get(t["symbol"])
            if m is None:
                continue
            # Inverse volume is quote notional; inverse turnover is base currency.
            volume = t["volume24h"] if category == "inverse" else t["turnover24h"]
            rows.append(row(m["baseCoin"] + m["quoteCoin"], m["baseCoin"], m["quoteCoin"],
                            t["prevPrice24h"], t["lastPrice"], volume,
                            market_symbol=t["symbol"], market_type=category))
    return rows


def hyperliquid(get):
    meta, contexts = get("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"})
    if len(meta["universe"]) != len(contexts):
        raise ValueError("Hyperliquid metadata/context length mismatch")
    return [row(m["name"].upper() + "USDC", m["name"].upper(), "USDC",
                t["prevDayPx"], t["markPx"], t["dayNtlVlm"], market_symbol=m["name"])
            for m, t in zip(meta["universe"], contexts) if not m.get("isDelisted", False)]


def fetch_markets(exchange, get=request_json):
    adapters = {"bitunix": bitunix, "binance": binance, "bybit": bybit, "hyperliquid": hyperliquid}
    if exchange not in adapters:
        raise ValueError(f"Unsupported EXCHANGE: {exchange}")
    try:
        rows = adapters[exchange](get)
        if not rows:
            raise ValueError(f"{exchange} returned no active perpetuals")
        return rows
    except (KeyError, TypeError, IndexError) as error:
        raise ValueError(f"Invalid {exchange} market response") from error


def fetch_hour_high(exchange, symbol, market_type, hour_end, get=request_json):
    """Return the trade-price high for the UTC hour ending at hour_end (seconds)."""
    end = int(hour_end) * 1000
    start = end - 3_600_000
    try:
        if exchange == "bitunix":
            params = urlencode({"symbol": symbol, "interval": "1h", "endTime": end,
                                "limit": 1, "type": "LAST_PRICE"})
            payload = get("https://fapi.bitunix.com/api/v1/futures/market/kline?" + params)
            if payload["code"] != 0:
                raise ValueError("Bitunix kline API error")
            highs = [c["high"] for c in payload["data"] if int(c["time"]) == start]
        elif exchange == "binance":
            if market_type not in ("fapi", "dapi"):
                raise ValueError("Missing Binance contract market")
            params = urlencode({"symbol": symbol, "interval": "1h", "startTime": start,
                                "endTime": end - 1, "limit": 1})
            candles = get(f"https://{market_type}.binance.com/{market_type}/v1/klines?" + params)
            highs = [c[2] for c in candles if int(c[0]) == start and int(c[6]) == end - 1]
        elif exchange == "bybit":
            if market_type not in ("linear", "inverse"):
                raise ValueError("Missing Bybit contract category")
            params = urlencode({"category": market_type, "symbol": symbol, "interval": "60",
                                "start": start, "end": end - 1, "limit": 1})
            payload = get("https://api.bybit.com/v5/market/kline?" + params)
            if payload["retCode"] != 0:
                raise ValueError("Bybit kline API error")
            highs = [c[2] for c in payload["result"]["list"] if int(c[0]) == start]
        elif exchange == "hyperliquid":
            candles = get("https://api.hyperliquid.xyz/info", {
                "type": "candleSnapshot", "req": {"coin": symbol, "interval": "1h",
                                                "startTime": start, "endTime": end - 1}})
            highs = [c["h"] for c in candles if int(c["t"]) == start and int(c["T"]) == end - 1
                     and c["s"] == symbol and c["i"] == "1h"]
        else:
            raise ValueError(f"Unsupported EXCHANGE: {exchange}")
        if len(highs) != 1:
            raise ValueError("Expected one candle for the last closed UTC hour")
        high = Decimal(highs[0])
        if not high.is_finite() or high <= 0:
            raise ValueError("Invalid hourly high")
        return high
    except (KeyError, TypeError, IndexError, InvalidOperation, OverflowError) as error:
        raise ValueError(f"Invalid {exchange} candle response") from error
