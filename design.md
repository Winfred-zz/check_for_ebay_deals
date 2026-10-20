# check_for_ebay_deals — design record

Status: implementation in progress against `handoff/0001.md`. This file records
approved requirements, the architecture for the remediation, and the testing
strategy. User-facing setup lives in `README.md`; implementation notes do not.

## Approved behavioral contract (owner-approved, from handoff 0001)

| Area | Decision |
|---|---|
| Alert eligibility | One alert per item ID, globally across searches. A lower price for an already-alerted item does not re-alert. |
| Selection | Per scan, per search row: the cheapest previously-unalerted valid listing at or below the ceiling. A more expensive item may alert on a later scan. |
| Quiet hours | UTC. Suppress alert delivery from 02:00 inclusive until 12:00 exclusive; 12:00 allows delivery. Scanning still runs during quiet hours; delivery of what was found is suppressed, and found items are queued as pending and delivered when the window opens (re-validated: a pending alert is delivered as-is). |
| Money | USD only. Search results must parse as exact USD amounts (`$1,234.56`, optional `US ` prefix tolerated only when the value itself is USD; ranges and other currencies rejected as unsupported). Ceilings are decimal minor units, non-negative, compared inclusive, exclude tax. Free shipping stays a search filter. |
| Condition | Exclude listings explicitly marked "Parts Only" (subtitle) — the existing intent; do not exclude a listing just because a subtitle is missing. |
| Delivery | At-least-once. Failed sends stay pending for retry; a crash after send but before recording success may duplicate (documented small window). Never record a failed send as sent. |
| Cadence | One scan per hour, finish-to-start scheduling with no overlap; bounded per-search work; cancellation honored. |
| Failures | Configuration errors stop startup with actionable messages and nonzero exit. Transient HTTP/retry-exhaustion failures for one search are recoverable and logged; they must not silently become "no matches" successes for that scan and monitoring continues. |
| Instance | Single running instance enforced by an exclusive `flock` on `bot.lock` (kernel-released on holder death; no stale-PID probing); a second instance exits 3. Concurrency support is explicitly out of scope. |
| Deployment | Primary model: self-contained Docker image (application copied in), plus documented direct Python install. State/logs in a user-writable volume `/data` (non-root runtime user). Shutdown bound: 10 seconds on SIGTERM. |
| Operational status | Bot currently stopped/not deployed (owner-reported). |

## Architecture

```
check_for_ebay_deals.py   entry point: CLI parsing, config validation, logging
                          setup once, SIGTERM handling, Discord lifecycle,
                          hourly scan loop with pending-alert delivery
config.py                 Config/ConfigError: reads .env (optional), CSV,
                          validates headers/rows/ceilings, path overrides
acquire.py                fetch_search_page_async(): aiohttp fetch under a
                          per-search deadline; status classification, bounded
                          cancellable retry, Retry-After parsing, cancel_event
parse.py                  parse_search_page(): single-pass BeautifulSoup parse
                          returning cards + a layout classification (deny /
                          no-results / results); USD parsing, condition
                          exclusion, ID extraction, card dedup (cheapest wins)
selection.py              choose_alerts(): deterministic cheapest-eligible
                          selection given parsed items, ceilings and sent
                          set; tie handled by first-seen stable order
clock.py                quiet-window predicate on injected clock (UTC
                          ZoneAware); DST-safe; 02:00<=h<12:00 quiet
state.py                SQLite state store: sent item IDs, pending alerts
                          (id, message, discovered_at), migrated
                          from configs/skip_items.txt (backup kept, not
                          committed); atomic transitions; refuse corrupt
notify.py                 render_alert_message(): markdown-escaped friendly
                          name, validated https ebay URL, no mentions
                          (suppression), Discord length caps without
                          truncating URLs
logs:  logging configured once in main under the state root
       (--data-dir/logs/bot.log); setup_logging is invocation-idempotent
       (removes its own tagged handlers first), console INFO / file DEBUG+
       rotating, uncaught exceptions logged CRITICAL before exit
```

State database: `data/state.sqlite` (path configurable, schema version 1).
Pending alerts are persisted before send; mark-sent happens only after a
successful Discord send (crash after send, before mark-sent, replays the
row: at-least-once, never marks a failed send). Legacy `configs/skip_items.txt`
IDs are imported into `sent` with INSERT-OR-IGNORE, so re-opening is
idempotent and the legacy file is read, never written, never deleted (CR06).

## Testing strategy

- Unit: parse (fixtures incl. valid/absent subtitle, "Parts Only",
  malformed cards, seller-info leaks, USD/currency/range/malformed prices,
  malformed+duplicate IDs, malformed card then valid card, empty page,
  unexpected layout marker); acquire (mocked session: statuses 200/404/
  429+Retry-After/5xx/timeout/connection, retry exhaustion, cancel — no
  real sleeps); select (below/equal/above ceiling, skipped cheapest,
  already-alerted price drop, later cheaper, duplicate cross-search,
  ties, empty); clock (01:59/02:00/11:59/12:00/12:59/13:00, UTC fixed
  clock; DST: UTC never shifts — assert explicit injected-clock DST
  boundary cases for the configured zone logic incl. Europe/London-style
  behavior via a tz-aware test clock).
- Integration (offline): state (failed send stays pending, restart
  resumes, partial batch failure, corrupt-state refusal, legacy
  migration, disk write failure, lockfile instance enforcement);
  notify (mention-like names escaped, oversized text split without
  breaking URLs).
- End-to-end offline: CLI `--once --dry-run --fixture-dir` runs
  production pipeline with local fixture HTTP and non-sending sink;
  asserts emitted alert identities/amounts, quiet-window suppression
  with controlled clock, unchanged sent state.
- Process-level: SIGTERM bounded shutdown exercised; invalid config
  exits nonzero with actionable message from a foreign cwd; import has
  no side effects.
- Manual/exploratory: fixture-driven runs against temporary dirs;
  loopback HTTP server (localhost only) for one acquisition integration;
  live Discord/eBay check only with separate authorization (expected
  unavailable → recorded limit).
- No coverage gates; no tests of source strings/copied config/
  incidental defaults/mock echo forwarding.

## Decisions/dispositions for smaller findings

- F24: command/error boilerplate removed (bot defines no commands;
  supported interface is scanning only). Annotations corrected.
- F25: `pages` removed from the runtime path (callers never pass it;
  single page per search retained, documented). `completed` option
  dropped (no caller uses it).
- F28: `.vscode/launch.json` is being untracked and deleted; editor
  config stays local per project-defaults convention.
- F21: tracked files under `configs/` (search CSV, skip list) untracked
  and preserved locally; a tracked example moves to `examples/`
  (outside ignored parent). Skip list is runtime state: untracked.
- F27: Dockerfile rebuilt self-contained (copies app, installs from
  requirements), python3.12 base, non-root user, no git layer,
  `.dockerignore` added.
- Logging: keep `logs/` directory and standard-library rotating
  handler (single process instance; the shared multiprocess helper is
  not required because of single-instance enforcement). Console INFO,
  file DEBUG+, retention documented as maxBytes/backupCount actuals.
- F26: Discord lifecycle = login ONCE in main, then a supervised
  background task `connect(reconnect=True)`; `wait_until_ready()` under a
  bounded timeout gates the first scan (ready banner logs only after
  readiness; login rejection -> exit 4, gateway never ready in window ->
  exit 1). Per-pass `fetch_channel(channel_id)` replaces any cached-channel
  assumption (Forbidden/NotFound/HTTPException logs and leaves pending
  alerts pending). Shutdown: SIGTERM requests cooperative cancellation
  inside the running scan/send; the drain is bounded (10s), the gateway
  task + client + session are closed, the flock is released, and pending
  rows stay durable. The token value is never echoed in logs or errors.
