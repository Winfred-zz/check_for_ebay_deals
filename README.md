# check_for_ebay_deals

Monitors configured eBay searches hourly and sends one alert per qualifying
item to a single Discord channel. Live searches use eBay's official Browse API;
offline fixtures use saved HTML and never access eBay.

## Setup

Python 3.12. Create a virtual environment and install the pinned deps:

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Configure two things (both gitignored user-local files):

1. **Search list** — `configs/ebay_deals_to_check.csv`, headers exactly
   `max_price,friendly_name,search_query`. Copy
   `examples/ebay_deals_to_check.csv` as the starting shape; keep your
   real searches out of the repo.
2. **Discord target** — `DISCORD_TOKEN` and `CHANNELID` (numeric channel
   id) in the process environment or an `.env` file passed via
   `--env-file`. Template: `examples/.env.example`. Never commit a real
   token; the app never prints the token value.

3. **eBay production API** — create production application keys in the eBay
   Developer Program. Store both App ID (Client ID) and Cert ID (Client Secret)
   as JSON in `~/.config/check-for-ebay-deals/ebay-production.json`, owned by
   you with mode `0600`; the parent directory should be `0700`. Never commit or
   paste these values. Override the path with `--ebay-credentials PATH` or
   `EBAY_CREDENTIALS_FILE`. The app requests an application token in memory;
   it never logs or persists the token.

You must create the Discord bot and invite it to the channel yourself
(<https://realpython.com/how-to-make-a-discord-bot-python/>).

## Running

```
.venv/bin/python check_for_ebay_deals.py                          # hourly loop
.venv/bin/python check_for_ebay_deals.py --once                   # single scan (cron)
.venv/bin/python check_for_ebay_deals.py --dry-run                # print alerts, no sends/writes
.venv/bin/python check_for_ebay_deals.py --once --dry-run \
  --fixture-dir tests/fixtures/offline-scan --csv \
  tests/fixtures/offline-scan/smoke.csv --data-dir /tmp/deals-fx   # offline fixture scan
```

Flags: `--csv PATH`, `--data-dir DIR`, `--env-file FILE`,
`--ebay-credentials PATH` (or `EBAY_CREDENTIALS_FILE`), `--at ISO-UTC`
(deterministic clock, e.g. `2026-10-19T13:00:00Z`), `--once`,
`--dry-run`, `--fixture-dir DIR` (offline fixture scans never contact
eBay).

Behavior:

- Quiet hours 02:00–12:00 UTC: scans still run and new alerts are stored
  as pending; delivery attempts are zero.
- Outside quiet hours, stored pending alerts are delivered first
  (at-least-once: a failed send leaves rows pending; the next run
  retries them).
- State lives in the `--data-dir` root (default `data/`): `state.sqlite`,
  `bot.lock`, and `logs/bot.log`. A `flock` on `bot.lock` gives single
  instance; a second run exits `3`. The kernel releases the lock when the
  holder dies, so a crashed instance never leaves a stale lock.
- `SIGTERM` cancels the running scan within ~10s and releases the lock;
  pending rows stay durable — a restart resumes them.

Exit codes: `0` normal, `2` config/state refused at startup
(actionable message on stderr), `3` another instance holds the lock,
`4` Discord login rejected, `5` required `--once` work failed (failed
searches or persistent send failures; rows stay durable for the next run),
`1` unexpected failure (logged, never swallowed).

Alerts are capped at Discord's 2000-character limit: friendly names are
Markdown-escaped and trimmed so the item URL always stays byte-intact.

## Offline testing

`--fixture-dir DIR` scans local HTML (`manifest.json` maps each CSV query
to a page file) and never contacts eBay. Non-dry fixture runs require an
explicit `--data-dir` (they must not write the real state root). Stub
knobs apply only to fixture runs: `CHECK_FOR_EBAY_DEALS_STUB_FAIL_AFTER=N`
(fail sends after N), `CHECK_FOR_EBAY_DEALS_STUB_BLOCK_SECONDS=S` (block
each send S s), `CHECK_FOR_EBAY_DEALS_SCAN_SLEEP_SECONDS=S` (pause S s
before fetching) — provided for shutdown/at-least-once tests.

## Running with Docker

Self-contained image (CSV and secrets mounted read-only; state/logs on a
named volume):

```
docker build -t check_for_ebay_deals .
docker run --rm \
  --env-file .env \
  -v "$HOME/.config/check-for-ebay-deals/ebay-production.json:/run/secrets/ebay-production.json:ro" \
  -v deals-data:/data \
  -v "$PWD/configs:/data/configs:ro" \
  check_for_ebay_deals --ebay-credentials /run/secrets/ebay-production.json
```

`/data` holds state (`/data/state.sqlite`), lock (`/data/bot.lock`), and
logs (`/data/logs`); the bound `configs/` supplies the search CSV.

## Limits

Live acquisition uses one Browse API search page per unique query, sorted by
price. It requests fixed-price US listings with free shipping, then locally
validates USD price, item URL/identity, condition and the CSV price ceiling.
The production path does not scrape HTML and has no fallback when the API
rejects a request. The public repo carries no tokens, personal configs, or CI
that can authorize a live check. See `design.md` for the full design record.
