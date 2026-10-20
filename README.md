# check_for_ebay_deals

Checks configured eBay searches once per hour and posts one alert per
search to a single Discord channel. Alert decisions are computed
locally — nothing renders a human decision at runtime.

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

Flags: `--csv PATH`, `--data-dir DIR`, `--env-file FILE`, `--at ISO-UTC`
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

Self-contained image (user configs bind-mounted; state/logs on a
volume, created with an image-baked ancestor layer so ownership survives
the anonymous-volume copy-up):

```
docker build -t check_for_ebay_deals .
docker run --rm \
  --env-file .env \
  -v deals-data:/data \
  -v "$PWD/configs:/data/configs:ro" \
  check_for_ebay_deals
```

`/data` holds state (`/data/state.sqlite`), lock (`/data/bot.lock`), and
logs (`/data/logs`); the bound `configs/` supplies the search CSV.

## Limits

eBay scraping is unofficial: deny pages are detected and skipped
(monitoring continues), and scans stay single-page, one search per row.
The public repo carries no tokens, no personal configs, and no CI that
could authorize the live check. See `design.md` for the full design
record.
