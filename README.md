# Shortbot

Identifies potential crypto short opportunities on Bitunix, Binance, Bybit, or
Hyperliquid and sends alerts through ntfy. Does not place orders and uses public
market data without an API key. Requires Python 3.11+; no third-party Python packages.

## Behavior

- Checks on the hour, starting at the next full hour after startup.
- Uses current rolling 24-hour perpetual market statistics, not an exact
  historical candle-close calculation.
- Default filters: price increase **greater than 100%** and 24-hour turnover of
  **at least 10,000,000 USD-equivalent**. Both thresholds are configurable.
- Sends at most one alert per coin in a rolling 24-hour window. If the coin still
  qualifies after its cooldown, it triggers an alert at the next hourly check.
  The same base coin shares its cooldown across quote currencies and exchanges;
  existing Bitunix cooldowns remain valid. Different coin names or contract
  multipliers (such as PEPE versus 1000PEPE) are not automatically treated as equivalent.
- Persists cooldowns in a SQLite file on a Docker volume.
- Uses the title **💩 Shortbot**. Each notification contains a Markdown
  exchange/ticker link, the percentage increase, and USD-equivalent turnover
  without a currency label. Only the ticker link opens TradingView; no extra
  buttons are added. The ntfy client determines whether the link opens in a new
  tab. Support for Markdown in message bodies varies between ntfy clients.

The API call starts on the hour. API, network, and push delays mean delivery
exactly at `HH:00:00` is not guaranteed. Scheduling and logs use UTC by default;
`TZ` determines local hour boundaries, including half-hour time zones and DST.

## Configuration

Set these values under `environment` in `compose.yaml` (or through `.env`):

```yaml
EXCHANGE: bitunix
QUOTE_CURRENCIES: "USDT,USD,USDC,BTC"
INCREASE_THRESHOLD: "100"
VOLUME_THRESHOLD: "10000000"
TZ: UTC
```

- `EXCHANGE`: `bitunix`, `binance`, `bybit`, or `hyperliquid`; one at a time.
- `QUOTE_CURRENCIES`: comma-separated actual quote currencies. For example,
  `BTC` means ETH/BTC, not BTC/USDT or a contract settled in BTC. Only pairs
  actually offered as perpetuals by the exchange are included. Supported values:
  USD, USDT, USDC, BUSD, FDUSD, TUSD, DAI, USDE, and USD1.
- `INCREASE_THRESHOLD`: percentage without a % sign; comparison is strictly `>`.
- `VOLUME_THRESHOLD`: USD-equivalent; comparison is `>=`.
- `TZ`: IANA time zone, such as `UTC` or `Europe/Amsterdam`.

For the volume filter, USD stablecoins are treated as nominally worth 1 USD
(no depeg adjustment). BTC-quoted turnover is converted using the current BTC
price from a USD-related perpetual pair on the same exchange. If that price is
unavailable, the BTC pair is skipped with a warning. Turnover is never aggregated
across different pairs.

Bitunix uses `lastPrice`, `open`, and `quoteVol`. Binance uses active USD-M/COIN-M
perpetuals; for COIN-M, USD turnover equals the number of contracts multiplied by
the contract size. Bybit uses active linear/inverse perpetuals, including
paginated instrument information. For inverse Bybit contracts, `volume24h` is
quote turnover, not `turnover24h` (base currency). Expiring futures and spot
markets are excluded.

Hyperliquid uses its native perpetual market (not HIP-3 builder markets),
`markPx / prevDayPx` for the price increase, and `dayNtlVlm` for USD notional
volume. This market is USDC-quoted; an example TradingView ticker is
`HYPERLIQUID:BTCUSDC.P`. Other exchanges use their own prefixes. Availability of
every new or small pair on TradingView is not guaranteed.

## Installing with Docker

Requirements: Docker Engine with Compose v2 and internet access. This repository
and its GHCR package are public; no login is required. The same image supports
`linux/amd64` (Intel/AMD) and `linux/arm64` (including Raspberry Pi with a 64-bit
OS). Docker automatically selects the correct variant. A 32-bit Pi OS is not
supported. The bot does not require an inbound port.

1. Clone the project and prepare the configuration:

```sh
git clone https://github.com/snailBoob/shortbot.git
cd shortbot
cp .env.example .env
```

2. Edit `.env`: configure the exchange, quote currencies, thresholds, time zone,
   and especially `NTFY_SERVER`/`NTFY_TOPIC` for your environment. Subscribe to
   that server and topic in your ntfy client. The default server is `https://ntfy.sh`.
   `NTFY_TOPIC` is required and has no default: choose your own topic using 1–64
   letters, digits, hyphens, or underscores. The bot will not start without a
   topic. Other people can read from and publish to a public topic; a hard-to-guess
   topic name is not a substitute for access controls.

   For a server with authenticated publishing, set `NTFY_TOKEN` to a token for
   an ntfy user with write access to the chosen topic. Leave it empty to publish
   without authentication. Use HTTPS outside a trusted internal network. Store
   the token only in your local `.env`, never in Git. Token support is available
   starting with `v0.2.0`.

   To allow public reading and authenticated publishing on your own ntfy server,
   give anonymous users read-only access to the topic and the token user
   write-only access. Sending a token does not automatically configure these
   server permissions.

3. Pull the image, check market data without sending notifications, and start the bot:

```sh
docker compose pull
docker compose run --rm shortbot python shortbot.py --dry-run
docker compose up -d
docker compose logs -f shortbot
```

Compose defaults to `ghcr.io/snailboob/shortbot:latest`. To download it directly:

```sh
docker pull ghcr.io/snailboob/shortbot:latest
```

To pin a version, set `SHORTBOT_IMAGE=ghcr.io/snailboob/shortbot:v0.2.0` in `.env`.

The startup log shows the exchange, quote currencies, and first scheduled check.
Compose uses the Docker host's default logging driver. On Synology, this is
usually `db`: explicitly using `json-file` may make logs available through
`docker logs` but leave them absent from Container Manager. Keep the host's
default logging driver. Log rotation and storage limits follow the host
configuration; this Compose configuration does not impose its own limit.
A real notification is sent only when a pair meets the filters. Run one bot
instance per channel. Stop local test instances before starting production;
otherwise, each instance has its own cooldown storage.
After changing the configuration, run `docker compose up -d`.

To stop: `docker compose down`. Do not use `down -v` if you want to preserve
cooldowns: it deletes the volume. Back up the volume while the bot is stopped.

### App suite integration

Copy the service and volume from `compose.yaml` into your app suite. Use
`ghcr.io/snailboob/shortbot:v0.2.0`, inject the environment variables, and mount
a persistent volume at `/data`. The container runs as UID 10001; when using a
bind mount, that user must have write access to the host directory. The default
named volume handles this on first startup. No port mappings are required.
The bot stops on SIGTERM and reads its configuration at startup.

### Updating and rolling back

Set `SHORTBOT_IMAGE` in `.env` to a new explicit release tag, then run:

```sh
docker compose pull
docker compose up -d
docker compose logs --tail=50 shortbot
```

To roll back, set `SHORTBOT_IMAGE` to the previous tested release tag and run
the same commands. Preserve the data volume and check future release notes for
data compatibility. `v0.2.0` is the first release of this public repository.
For an exact pin, you can also use `ghcr.io/snailboob/shortbot@sha256:...`.

### Building locally

```sh
docker build -t shortbot:local .
SHORTBOT_IMAGE=shortbot:local docker compose up -d
```

## Releases and verification

Versions are recorded as Git tags (`v0.2.0`). On pushes and pull requests,
GitHub Actions runs unit/HTTP integration tests and builds both architectures.
It also runs the tests and a non-root SQLite volume check in both images,
without making real exchange or ntfy calls. ARM checks run through QEMU, not
on a physical Raspberry Pi. Users test their actual production hosts.

Only a `vX.Y.Z` tag publishes the tested images to GHCR. Each release has a
version tag and a `sha-<commit>` tag. After the checks, `latest` points to the
new release. The pipeline checks public visibility and downloads both
architectures without a registry login. The manifest digest is recorded in the
Actions run. Never reuse a release tag for changed code; create a new version.
Images remain available for installation and rollback until the owner explicitly
retires the release; only then should its version, commit, and architecture tags
be deleted. There are no temporary CI artifact uploads. The pipeline publishes
a package but does not start or replace any production containers.

The private development repository is named `shortbot-dev` and uses a separate
container package. This public repository starts with a clean Git history.

## Running locally and checking behavior

```sh
python3 -m unittest discover -s tests -v
python3 shortbot.py --dry-run
EXCHANGE=bybit QUOTE_CURRENCIES=USDC python3 shortbot.py --dry-run
python3 shortbot.py
```

Dry-run fetches real market data and prints matches, but sends nothing and does
not write state. The default local state path is `data/shortbot.sqlite3`;
override it with `STATE_PATH`. For local execution, set environment variables
in your shell; Python does not automatically load `.env`.

The tests use a local HTTP server for Bitunix and ntfy responses; they do not
send real notifications.

## Errors and delivery guarantees

API errors are logged, and the bot retries at the next full hour. Missed hours
are not replayed. Invalid ticker values are skipped.

A delivery attempt is durably reserved **before** the HTTP call. On a timeout,
ambiguous server response, or process failure, the 24-hour cooldown remains in
place to avoid repeating a notification that may already have been delivered.
This can mean a notification is lost and is not retried until 24 hours later.
A definitive HTTP 4xx rejection (except 408) releases the reservation; the next
attempt happens at the next hourly check. Once delivery is acknowledged, the
cooldown starts at the acknowledgment time. Errors appear in the container logs.

## API references

- [Bitunix Get Tickers](https://www.bitunix.com/api-docs/futures/market/get_tickers.html)
  — `GET https://fapi.bitunix.com/api/v1/futures/market/tickers` retrieves all
  tickers. The bot filters locally; the endpoint has no percentage-increase filter.
- [Publishing to ntfy](https://docs.ntfy.sh/publish/)
- [Binance USD-M](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/24hr-Ticker-Price-Change-Statistics)
- [Bybit tickers](https://bybit-exchange.github.io/docs/v5/market/tickers)
- [Hyperliquid perpetuals](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
