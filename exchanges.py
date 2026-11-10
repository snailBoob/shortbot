"""Public perpetual market data, normalized at the exchange boundary."""

import json
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

EXCHANGES = ("bitunix", "binance", "bybit", "hyperliquid")
USD_QUOTES = frozenset(("USD", "USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDE", "USD1"))


def request_json(url, body=None):
    request = Request(url, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json", "User-Agent": "Shortbot/1.0"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def row(symbol, base, quote, opening, price, volume):
    return {"symbol": symbol, "base": base, "quote": quote, "open": opening,
            "lastPrice": price, "quoteVol": volume}


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
                            t["openPrice"], t["lastPrice"], volume))
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
                            t["prevPrice24h"], t["lastPrice"], volume))
    return rows


def hyperliquid(get):
    meta, contexts = get("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"})
    if len(meta["universe"]) != len(contexts):
        raise ValueError("Hyperliquid metadata/context length mismatch")
    return [row(m["name"].upper() + "USDC", m["name"].upper(), "USDC",
                t["prevDayPx"], t["markPx"], t["dayNtlVlm"])
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
