# Shortbot

Signaleert mogelijke crypto-shorts op Bitunix, Binance, Bybit of Hyperliquid via
ntfy. Plaatst geen orders en gebruikt publieke marktdata zonder API-key.
Python 3.11+; geen externe Python-packages.

## Gedrag

- Controle op ieder heel uur, eerste controle op het volgende hele uur na starten.
- Actuele, voortschrijdende 24-uursstatistieken van perpetuals; geen exacte
  historische candle-closeberekening.
- Standaard stijging **meer dan 100%** en 24-uursomzet **minimaal 10.000.000
  USD-equivalent**. Beide drempels zijn instelbaar.
- Maximaal één alert per coin per voortschrijdende 24 uur. Als de coin na de
  cooldown nog steeds voldoet, volgt bij de eerstvolgende uurcontrole een alert.
  Dezelfde base coin deelt zijn cooldown over noemers en exchanges heen; bestaande
  Bitunix-cooldowns blijven geldig. Verschillende coin-namen of contractmultipliers
  (zoals PEPE versus 1000PEPE) worden niet automatisch gelijkgesteld.
- Cooldowns blijven bewaard in een SQLite-bestand op een Docker-volume.
- De titel is **💩 Shortbot**. De notificatie toont een Markdown-link met
  exchange/ticker, het percentage en de USD-equivalente omzet zonder valutalabel.
  Alleen de tickerlink opent TradingView; er worden geen extra knoppen toegevoegd.
  Of de link in een nieuw tabblad opent, wordt door de ntfy-client bepaald.
  Ondersteuning voor Markdown in de berichttekst verschilt per ntfy-client.

De API-call start op het uur; API-, netwerk- en pushvertraging betekenen dat
ontvangst exact om `HH:00:00` niet gegarandeerd is. Planning en logs gebruiken
standaard UTC; `TZ` bepaalt de lokale uurgrenzen, ook voor halfuurzones en DST.

## Instellingen

In `compose.yaml` onder `environment` (of via `.env`):

```yaml
EXCHANGE: bitunix
QUOTE_CURRENCIES: "USDT,USD,USDC,BTC"
INCREASE_THRESHOLD: "100"
VOLUME_THRESHOLD: "10000000"
TZ: UTC
```

- `EXCHANGE`: `bitunix`, `binance`, `bybit` of `hyperliquid`; één tegelijk.
- `QUOTE_CURRENCIES`: komma-gescheiden echte noemers. `BTC` betekent bijvoorbeeld
  ETH/BTC, niet BTC/USDT of een contract dat in BTC wordt afgerekend. Alleen paren
  die de exchange daadwerkelijk aanbiedt als perpetual worden meegenomen.
  USD, USDT, USDC, BUSD, FDUSD, TUSD, DAI, USDE en USD1 worden ondersteund.
- `INCREASE_THRESHOLD`: percentage zonder %-teken; vergelijking is strikt `>`.
- `VOLUME_THRESHOLD`: USD-equivalent; vergelijking is `>=`.
- `TZ`: IANA-tijdzone, bijvoorbeeld `UTC` of `Europe/Amsterdam`.

USD-stablecoins worden voor de volumefilter nominaal als 1 USD behandeld (geen
depegcorrectie). BTC-genoteerde omzet wordt omgerekend met een actuele BTC-prijs
uit een USD-gerelateerd perpetual-paar op dezelfde exchange. Ontbreekt die prijs,
dan wordt het BTC-paar overgeslagen met een waarschuwing. Omzet wordt nooit over
verschillende paren opgeteld.

Bitunix gebruikt `lastPrice`, `open` en `quoteVol`. Binance gebruikt actieve
USD-M/COIN-M perpetuals; bij COIN-M is USD-omzet het aantal contracten maal
contractgrootte. Bybit gebruikt actieve lineaire/inverse perpetuals, inclusief
gepagineerde instrumentinformatie. Voor inverse Bybit-contracten is `volume24h`
de quote-omzet, niet `turnover24h` (base currency). Expiry-futures en spot vallen
buiten de selectie.

Hyperliquid gebruikt de native perpetual-markt (geen HIP-3 builder-markten),
`markPx / prevDayPx` voor de stijging en `dayNtlVlm` voor USD-notional volume.
Deze markt is USDC-genoteerd; TradingView-tickers zijn bijvoorbeeld
`HYPERLIQUID:BTCUSDC.P`. De overige exchanges gebruiken hun eigen prefix.
Beschikbaarheid van ieder afzonderlijk nieuw of klein paar op TradingView is
niet gegarandeerd.

## Installeren met Docker

Benodigd: Docker Engine met Compose v2 en internettoegang. Deze repository en
het GHCR-package zijn publiek; inloggen is niet nodig. Hetzelfde image ondersteunt
`linux/amd64` (Intel/AMD) en `linux/arm64` (onder andere Raspberry Pi met 64-bit
OS). Docker selecteert automatisch de juiste variant. Een 32-bit Pi-OS wordt
niet ondersteund. De bot heeft geen inkomende poort nodig.

1. Haal het project op en stel de configuratie in:

```sh
git clone https://github.com/snailBoob/shortbot.git
cd shortbot
cp .env.example .env
```

2. Pas `.env` aan: exchange, noemers, drempels, tijdzone en vooral
   `NTFY_SERVER`/`NTFY_TOPIC` voor je eigen omgeving. Abonneer in de ntfy-client
   op die server en dat topic. De standaardserver is `https://ntfy.sh`.
   `NTFY_TOPIC` is verplicht en heeft geen standaardwaarde: kies je eigen topic
   van 1-64 letters, cijfers, koppeltekens of underscores. Zonder topic start de
   bot niet. Een openbaar topic kan door anderen worden gelezen en beschreven;
   een moeilijk te raden topicnaam is geen vervanging voor toegangsrechten.

   Voor een server met beveiligd publiceren: stel `NTFY_TOKEN` in op een token
   van een ntfy-gebruiker met schrijfrechten op het gekozen topic. Leeg laten
   behoudt publiceren zonder authenticatie. Gebruik HTTPS buiten een vertrouwd
   intern netwerk. Bewaar het token alleen in de lokale `.env`, nooit in Git.
   Tokenondersteuning is beschikbaar vanaf `v0.2.0`.

   Wil je vrij lezen en beveiligd publiceren op een eigen ntfy-server? Geef
   anonieme gebruikers alleen leesrechten op het topic en de tokengebruiker
   alleen schrijfrechten. Een token meesturen stelt deze serverrechten niet
   automatisch in.

3. Haal het image op, controleer de data zonder meldingen en start de bot:

```sh
docker compose pull
docker compose run --rm shortbot python shortbot.py --dry-run
docker compose up -d
docker compose logs -f shortbot
```

Compose gebruikt standaard `ghcr.io/snailboob/shortbot:latest`. Los downloaden:

```sh
docker pull ghcr.io/snailboob/shortbot:latest
```

Voor een vaste versie zet je `SHORTBOT_IMAGE=ghcr.io/snailboob/shortbot:v0.2.0`
in `.env`.

De opstartlog toont exchange, noemers en het eerste geplande controlemoment.
Compose gebruikt de standaardlogdriver van de Docker-host. Op Synology is dat
doorgaans `db`: met een expliciete `json-file`-driver kunnen logs wel via
`docker logs` beschikbaar zijn, maar ontbreken in Container Manager. Laat daarom
de logdriver op de hoststandaard staan. Logrotatie en opslaglimieten volgen dan
de hostconfiguratie; deze Compose-configuratie stelt zelf geen limiet in.
Er volgt alleen een echte melding wanneer een paar voldoet aan de filters.
Draai één botinstantie per kanaal. Stop een lokale testinstantie voordat je
productie start, anders hebben beide instanties hun eigen cooldownopslag.
Na een configuratiewijziging: `docker compose up -d`.

Stoppen: `docker compose down`. Gebruik geen `down -v` als je cooldowns wilt
behouden: dat verwijdert het volume. Back-up het volume wanneer de bot gestopt is.

### App-suite-integratie

Neem de service en het volume uit `compose.yaml` over in de app-suite. Gebruik
`ghcr.io/snailboob/shortbot:v0.2.0`, injecteer de omgevingsvariabelen en koppel
een persistent volume aan `/data`. De container draait als UID 10001; bij een
bind mount moet die gebruiker schrijfrechten hebben op de hostmap. De standaard
named volume regelt dat bij de eerste start. Er zijn geen poortmappings nodig.
Stoppen gebeurt met SIGTERM; configuratie wordt bij opstarten ingelezen.

### Updates en terugrollen

Zet `SHORTBOT_IMAGE` in `.env` op een nieuwe expliciete releasetag, daarna:

```sh
docker compose pull
docker compose up -d
docker compose logs --tail=50 shortbot
```

Voor rollback zet je `SHORTBOT_IMAGE` terug op de vorige geteste releasetag en
voer je dezelfde commando's uit. Behoud het datavolume en controleer bij toekomstige
releases de release notes op datacompatibiliteit. `v0.2.0` is de eerste release
van deze publieke repository. Voor exact vastzetten kan ook
`ghcr.io/snailboob/shortbot@sha256:...` gebruikt worden.

### Zelf bouwen

```sh
docker build -t shortbot:local .
SHORTBOT_IMAGE=shortbot:local docker compose up -d
```

## Releases en verificatie

Versies worden vastgelegd met Git-tags (`v0.2.0`). GitHub Actions voert bij
pushes en pull requests unit-/HTTP-integratietests uit en bouwt beide architecturen.
Ook in beide images worden de tests en een niet-root SQLite-volumecheck uitgevoerd,
zonder echte exchange- of ntfy-aanroepen. ARM-controles draaien via QEMU, niet op
een fysieke Raspberry Pi. De gebruiker test de uiteindelijke productiehost.

Alleen een `vX.Y.Z`-tag publiceert de al geteste images naar GHCR. Naast de
versietag is er een `sha-<commit>`-tag. Na de controles wijst `latest` naar de
nieuwe release. De pipeline controleert publieke zichtbaarheid en downloadt
beide architecturen zonder registry-login. De manifestdigest staat in de Actions-run.
Releasetags worden niet hergebruikt voor gewijzigde code; maak een nieuwe versie.
Images blijven beschikbaar voor installatie en rollback totdat de eigenaar de
release expliciet buiten gebruik stelt; verwijder alleen dan de bijbehorende
versie-, commit- en architectuurtags. Er zijn geen tijdelijke CI-artifactuploads.
De pipeline publiceert een package, maar start of vervangt geen productiecontainer.

De private ontwikkelrepository heet `shortbot-dev` en gebruikt een afzonderlijk
containerpackage. Deze publieke repository begint met een schone Git-historie.

## Lokaal en controles

```sh
python3 -m unittest discover -s tests -v
python3 shortbot.py --dry-run
EXCHANGE=bybit QUOTE_CURRENCIES=USDC python3 shortbot.py --dry-run
python3 shortbot.py
```

Dry-run haalt echte marktdata op en print matches, maar stuurt niets en schrijft
geen state. Lokaal is de standaard state `data/shortbot.sqlite3`; wijzigbaar via
`STATE_PATH`. Omgevingsvariabelen moeten lokaal in de shell worden ingesteld;
Python leest `.env` niet automatisch.

De tests gebruiken een lokale HTTP-server voor Bitunix- en ntfy-responses;
ze sturen geen echte notificaties.

## Fouten en aflevergaranties

API-fouten worden gelogd; de bot probeert opnieuw op het volgende hele uur.
Gemiste uren worden niet ingehaald. Ongeldige tickerwaarden worden overgeslagen.

Een verzendpoging wordt **voor** de HTTP-call duurzaam gereserveerd. Bij een
timeout, onduidelijke serverrespons of procesuitval blijft de 24-uurscooldown
staan: zo veroorzaakt een mogelijk al afgeleverde melding geen herhaling.
Dit kan betekenen dat een melding verloren gaat en pas na 24 uur opnieuw wordt
geprobeerd. Een definitieve HTTP 4xx-afwijzing (behalve 408) heft de reservering
op; een nieuwe poging volgt bij de volgende uurcontrole. Na bevestigde ontvangst
begint de cooldown op het bevestigingstijdstip. Fouten staan in de containerlogs.

## API-bronnen

- [Bitunix Get Tickers](https://www.bitunix.com/api-docs/futures/market/get_tickers.html)
  — `GET https://fapi.bitunix.com/api/v1/futures/market/tickers` haalt alle tickers
  op. De bot filtert lokaal; het endpoint heeft geen procentuele-stijgingsfilter.
- [ntfy publiceren](https://docs.ntfy.sh/publish/)
- [Binance USD-M](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/24hr-Ticker-Price-Change-Statistics)
- [Bybit tickers](https://bybit-exchange.github.io/docs/v5/market/tickers)
- [Hyperliquid perpetuals](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
