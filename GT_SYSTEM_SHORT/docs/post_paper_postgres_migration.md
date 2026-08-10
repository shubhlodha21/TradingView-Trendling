# Post-Paper PostgreSQL Migration — Design & Checklist

**Status:** PLANNED — execute after the 4-day paper test concludes and shows the strategy has edge.
**Author:** GT System
**Estimated effort:** 1-2 days for replicator + schema; +0.5 day for PG-backed aggregator.
**Drafted:** 2026-05-18 (pre-paper-launch)

---

## Why

The current system writes all observability data to local files on the EC2 instance:

| File | Purpose | Volume |
|---|---|---|
| `.gt_state_<SYM>_<CID>.json` | Engine state snapshot (atomic + fsync) | < 5 KB per save, ~10 saves/min |
| `data/audit/feed_<SYM>_<DATE>.csv` | Every tick from IBKR | ~50 MB/day per symbol |
| `data/audit/order_<SYM>_<DATE>.csv` | Every order event | ~5 KB/day |
| `data/audit/state_<SYM>_<DATE>.csv` | State transitions + 5s snapshots | ~500 KB/day |
| `data/audit/pnl_<SYM>_<DATE>.csv` | P&L snapshots (5s cadence) | ~500 KB/day |
| `data/alerts/alerts_<DATE>.jsonl` | All alerts (shared across symbols) | < 100 KB/day |
| `.gt_live_<SYM>_<CID>.json` | Live snapshot for aggregator (5 Hz) | < 5 KB per write |

This works fine for one bot on one machine. It hits these walls as we scale:

1. **Cross-machine analytics** — dashboards/notebooks on the laptop need to SSH + grep CSVs to query history.
2. **Multi-symbol consolidation** — combining N per-symbol files for "show me total P&L today" requires join code.
3. **Audit survives EC2 termination** — if the EC2 disk fails, 4 days of session data is gone.
4. **Cold-restart on a new machine** — losing the EC2 instance means restoring state files from backup before a new bot can resume.
5. **SQL query power** — slippage analysis, latency-by-hour, Sharpe — all painful against CSVs.

The Postgres migration addresses 1, 3, 4, 5 directly. 2 is also helped.

---

## Architectural Decision: Hybrid Replication (NOT full migration)

**Rejected: full migration** (engine writes directly to Postgres).
**Reason:** would couple the trading hot path to a network service. RDS hiccups, network partitions, or slow queries would block the engine. **Trading correctness must not depend on a remote database being healthy.**

**Chosen: hybrid replication.**

The engine keeps writing to local files exactly as it does today. A separate **replicator daemon** tails those files and asynchronously streams to Postgres. The bot has zero hot-path PG dependency.

```
┌────────────────────────── EC2 instance ─────────────────────────────┐
│                                                                      │
│   ┌──────────────────┐                                               │
│   │  run_live.py     │  writes (unchanged):                          │
│   │  (engine + dash) │    .gt_state_*.json     atomic + fsync        │
│   └──────────────────┘    .gt_live_*.json      atomic                │
│           │                data/audit/*.csv    batched threaded      │
│           ▼                data/alerts/*.jsonl appended              │
│   ┌──────────────────┐                                               │
│   │  pg_replicator   │  reads files, tails, batches, INSERTs         │
│   │   (this is new)  │  retries on PG outage, never blocks engine    │
│   └──────────┬───────┘                                               │
│              │ TCP                                                   │
└──────────────┼───────────────────────────────────────────────────────┘
               ▼
         ┌─────────────┐
         │  RDS PG     │  in same VPC, private subnet
         │  db.t4g.    │  ~5-10 ms RTT
         │  small      │  ← bot doesn't talk here directly
         └─────────────┘
               │
               ├── pgadmin / psql (you)
               ├── Grafana / Metabase boards
               ├── Aggregator v2 (PG-backed, no SSH required)
               └── Backtest harness (replay feed_ticks via SQL)
```

### Why this is the right tradeoff

| Concern | File-only (today) | Hybrid (this plan) | Full PG migration (rejected) |
|---|---|---|---|
| Trading hot-path latency | local SSD ~50 µs | local SSD ~50 µs (same) | PG roundtrip 1-10 ms |
| Trading correctness if DB is down | N/A | unaffected | bot blocks or drops fills |
| Cross-machine analytics | SSH+grep | SQL from laptop | SQL from laptop |
| Audit survives EC2 loss | no | yes (within replication window) | yes |
| Operational complexity | minimal | +1 daemon, +1 RDS | +1 RDS, +schema migrations in hot path |
| Implementation risk | n/a | low (additive) | high (touches engine, every fill, every state save) |

---

## Goals & Non-Goals

### Goals
- Stream all audit data (orders, state events, P&L snapshots, alerts, feed ticks) to Postgres within ~5 s of being written to disk.
- Mirror live engine state to Postgres at ~1 Hz cadence.
- Make all session data queryable via standard SQL from any machine that can reach the RDS endpoint.
- Allow the multi-symbol aggregator dashboard to read from Postgres instead of (or in addition to) files.
- Preserve every safety property of the current file-based system: atomic writes, fsync on state, exec_id dedup on orders.

### Non-Goals (out of scope for v1)
- **Multi-machine bot HA / leader election.** Different project. The bot still runs on a single EC2.
- **Synchronous PG writes in the engine.** Never. Replicator is always async.
- **Replacing the audit file writers.** Files remain authoritative. PG is the queryable replica.
- **Migrating historical CSV data.** A one-shot backfill script can be written, but day-1 PG only has data from when the replicator starts.
- **Postgres logical replication or CDC.** We're streaming application data, not row changes from another DB.

---

## Schema Design

Seven tables. Partition the high-volume tables by date for retention.

### `engine_state` — current bot state per symbol

Upserted on every state save. One row per (symbol, client_id). Lets a new bot resume from PG if local state file is lost.

```sql
CREATE TABLE engine_state (
    symbol           TEXT NOT NULL,
    client_id        INT NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL,
    state            TEXT NOT NULL,            -- TradeState enum value
    position_open    BOOLEAN NOT NULL,
    entry_price      NUMERIC(12, 4),
    stop_loss        NUMERIC(12, 4),
    highest_price    NUMERIC(12, 4),
    previous_breakout_level NUMERIC(12, 4),
    quantity         INT NOT NULL DEFAULT 0,
    trades_today     INT NOT NULL DEFAULT 0,
    wins             INT NOT NULL DEFAULT 0,
    losses           INT NOT NULL DEFAULT 0,
    pnl              NUMERIC(14, 4) NOT NULL DEFAULT 0,
    total_commission NUMERIC(12, 4) NOT NULL DEFAULT 0,
    pending_buy_commission NUMERIC(12, 4) NOT NULL DEFAULT 0,
    cycle_id         TEXT,
    pending_stop     JSONB,
    pending_exit_reason TEXT,
    PRIMARY KEY (symbol, client_id)
);
```

### `live_snapshots` — high-frequency live view

Upserted at 5 Hz from `.gt_live_<SYM>_<CID>.json`. Powers the PG-backed aggregator.

```sql
CREATE TABLE live_snapshots (
    symbol     TEXT NOT NULL,
    client_id  INT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    last       NUMERIC(12, 4),
    bid        NUMERIC(12, 4),
    ask        NUMERIC(12, 4),
    bid_size   INT,
    ask_size   INT,
    volume     BIGINT,
    open       NUMERIC(12, 4),
    high       NUMERIC(12, 4),
    low        NUMERIC(12, 4),
    rate       NUMERIC(8, 2),
    vwap       NUMERIC(12, 4),
    tick_rate  NUMERIC(8, 2),
    trade_rate NUMERIC(8, 2),
    bbo_rate   NUMERIC(8, 2),
    buy_pct    NUMERIC(5, 2),
    sell_pct   NUMERIC(5, 2),
    tape       JSONB,                          -- last 6 classified trades
    latency    JSONB,                          -- {p50_ms, p99_ms, max_ms}
    connected  BOOLEAN,
    paused     BOOLEAN,
    PRIMARY KEY (symbol, client_id)
);
```

### `orders` — full order event stream

Append-only. Critical: `exec_id` UNIQUE constraint enforces dedup at the DB level (defense in depth — replicator should also dedup, but a stuck retry won't double-insert).

```sql
CREATE TABLE orders (
    id            BIGSERIAL,
    ts            TIMESTAMPTZ NOT NULL,
    symbol        TEXT NOT NULL,
    client_id     INT NOT NULL,
    event         TEXT NOT NULL,               -- SUBMITTED, FILLED, REJECTED, CANCELLED
    order_id      TEXT NOT NULL,               -- our engine_id
    exec_id       TEXT,                        -- IBKR execution ID (NULL for non-FILLED events)
    side          TEXT NOT NULL,               -- BUY, SELL
    qty           INT NOT NULL,
    order_type    TEXT,                        -- MARKET, LIMIT, STOP, STOP_LIMIT
    limit_price   NUMERIC(12, 4),
    stop_price    NUMERIC(12, 4),
    signal_price  NUMERIC(12, 4),
    fill_price    NUMERIC(12, 4),
    slippage      NUMERIC(12, 4),
    commission    NUMERIC(10, 4),
    pnl           NUMERIC(14, 4),
    reason        TEXT,
    state_at_time TEXT,
    position_at_time TEXT,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

-- exec_id uniqueness for FILLED events only — partial index
CREATE UNIQUE INDEX idx_orders_exec_id ON orders (exec_id) WHERE exec_id IS NOT NULL;
CREATE INDEX idx_orders_symbol_ts ON orders (symbol, ts);
CREATE INDEX idx_orders_event_ts ON orders (event, ts) WHERE event IN ('REJECTED', 'CANCELLED');
```

### `state_events` — engine state transitions + periodic snapshots

```sql
CREATE TABLE state_events (
    id           BIGSERIAL,
    ts           TIMESTAMPTZ NOT NULL,
    symbol       TEXT NOT NULL,
    client_id    INT,
    event        TEXT NOT NULL,
    state        TEXT,
    ltp          NUMERIC(12, 4),
    prev_ltp     NUMERIC(12, 4),
    position_open BOOLEAN,
    entry_price  NUMERIC(12, 4),
    highest_price NUMERIC(12, 4),
    stop_loss    NUMERIC(12, 4),
    trigger_price NUMERIC(12, 4),
    breakout_level NUMERIC(12, 4),
    pnl          NUMERIC(14, 4),
    trades_today INT,
    wins         INT,
    losses       INT,
    config_trigger NUMERIC(12, 4),
    config_stop_pct NUMERIC(8, 6),
    config_qty   INT,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX idx_state_symbol_ts ON state_events (symbol, ts);
```

### `pnl_snapshots` — 5-second P&L cadence

```sql
CREATE TABLE pnl_snapshots (
    id              BIGSERIAL,
    ts              TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    state           TEXT,
    position_open   BOOLEAN,
    entry_price     NUMERIC(12, 4),
    current_price   NUMERIC(12, 4),
    unrealized_pnl  NUMERIC(14, 4),
    realized_pnl    NUMERIC(14, 4),
    total_pnl       NUMERIC(14, 4),
    wins            INT,
    losses          INT,
    trades_today    INT,
    comm_today      NUMERIC(12, 4),
    highest_price   NUMERIC(12, 4),
    stop_loss       NUMERIC(12, 4),
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX idx_pnl_symbol_ts ON pnl_snapshots (symbol, ts);
```

### `alerts` — shared cross-symbol alert log

```sql
CREATE TABLE alerts (
    id        BIGSERIAL PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL,
    code      TEXT NOT NULL,
    severity  TEXT NOT NULL,
    message   TEXT,
    ticker    TEXT,                            -- nullable; extracted from context.ticker if present
    context   JSONB,
    correlation_id TEXT
);

CREATE INDEX idx_alerts_ts ON alerts (ts);
CREATE INDEX idx_alerts_severity_ts ON alerts (severity, ts) WHERE severity IN ('HIGH', 'CRITICAL');
CREATE INDEX idx_alerts_ticker_ts ON alerts (ticker, ts) WHERE ticker IS NOT NULL;
```

### `feed_ticks` — high-volume tick stream (optional for v1)

This is the heaviest table by far (1-2M rows/day per symbol). Daily-partitioned, 30-day retention. **Defer to v2 if v1 timing is tight** — the other tables are higher-value and lower-volume.

```sql
CREATE TABLE feed_ticks (
    id            BIGSERIAL,
    ts            TIMESTAMPTZ NOT NULL,
    symbol        TEXT NOT NULL,
    last          NUMERIC(12, 4),
    last_size     NUMERIC(10, 0),
    last_exchange TEXT,
    last_conditions TEXT,
    bid           NUMERIC(12, 4),
    ask           NUMERIC(12, 4),
    bid_size      INT,
    ask_size      INT,
    volume        BIGINT,
    open          NUMERIC(12, 4),
    high          NUMERIC(12, 4),
    low           NUMERIC(12, 4),
    tick_type     TEXT,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX idx_feed_symbol_ts ON feed_ticks (symbol, ts);
```

### Partition management

Create a small background process (cron or `pg_cron` extension) that:
- Creates next day's partition by 23:00 ET (`CREATE TABLE ... PARTITION OF ...`)
- Drops partitions older than 30 days for `feed_ticks`, 365 days for `orders` + `state_events` + `pnl_snapshots`

---

## Replicator Daemon Design

New file: `pg_replicator.py`. Runs as a systemd service alongside the bot.

### Responsibilities

1. **State files (high frequency, small):** Poll `.gt_state_*_*.json` + `.gt_live_*_*.json` every 200 ms. UPSERT into `engine_state` / `live_snapshots` if file mtime is newer than last seen.

2. **Audit CSVs (moderate frequency, batched):** Tail each `data/audit/*.csv` file. Maintain a cursor per file (last byte offset replicated). Every 5 s, read from cursor to EOF, batch-INSERT into the appropriate table, advance cursor.

3. **Alerts JSONL:** Same pattern. Tail `data/alerts/alerts_<DATE>.jsonl`, batch-INSERT.

4. **Feed ticks (v2 only):** Same tail-and-batch pattern but at higher frequency (1 s) due to volume. Use `COPY` instead of `INSERT` for bulk efficiency.

### Failure handling

- **PG unreachable:** retry with exponential backoff (1s → 60s → cap). Keep cursors in memory; on PG recovery, replay from where we left off.
- **Replicator crash:** persist cursor offsets to a small `.pg_replicator_state.json` on local disk. On restart, resume from saved offsets.
- **Schema migration mid-session:** replicator should treat `relation does not exist` errors as transient (PG admin is migrating), back off, retry.
- **Order of operations:** within a single batch, INSERT `state_events` before `orders` so foreign keys (if added later) resolve correctly. v1 has no FKs across tables.

### Cursor file format

```json
{
  "audit/order_NVDA_20260520.csv": {"offset": 2048, "lines_inserted": 32},
  "audit/order_TSLA_20260520.csv": {"offset": 0, "lines_inserted": 0},
  "alerts/alerts_20260520.jsonl": {"offset": 1024, "lines_inserted": 12}
}
```

### Connection pooling

Use `psycopg[c]` (psycopg3 with C extension) or `asyncpg`. Single persistent connection per replicator process is enough for our write volume.

### Dedup strategy

The `exec_id` UNIQUE index on `orders` is the safety net. Replicator should also dedup using cursor offsets — replays from offset 0 after a crash should not produce duplicate rows because the offset is restored from `.pg_replicator_state.json` BEFORE we resume reading.

### Resource usage (estimated)

- Memory: ~20 MB Python process
- CPU: < 1% of one core
- Disk I/O: read-only on audit files (no contention with engine writers)
- Network: < 100 KB/s to PG under steady-state, peaks at 2-5 MB/s during feed_ticks bulk inserts

---

## RDS Sizing & Cost

| Component | Choice | Monthly cost (us-east-1) |
|---|---|---|
| RDS instance | `db.t4g.small` (2 vCPU, 2 GB RAM, ARM Graviton) | ~$25 |
| Storage | 100 GB gp3 SSD | ~$12 |
| Automated backups | 7-day retention (included) | $0 |
| Multi-AZ | **No** (single-AZ for cost; PG outage = analytics down but trading unaffected) | $0 |
| Data transfer | < 1 GB/day, within same VPC = free | $0 |
| **Total** | | **~$37/mo** |

For one bot. Scales linearly with symbol count via tick volume.

**Alternative for lower cost:** self-hosted PG on the same EC2 instance (~$0 incremental). Trade-off: no separation between trading and analytics — if the bot OOMs, PG is gone too. Defeats half the purpose.

**Alternative for higher resilience:** Supabase, Neon, or RDS Multi-AZ (~$60-80/mo). For early-stage live trading, single-AZ RDS is fine.

---

## Aggregator Migration (Optional Phase 2)

Once the replicator is running, the multi-symbol aggregator dashboard can switch its data source from local files to Postgres. Benefits:
- Run aggregator from your laptop, no SSH required.
- Query historical sessions (drill into yesterday's NVDA trades).
- Multiple viewers don't add load to the trading EC2.

### Changes needed in `dashboard_agg.py`

| Currently reads from | Switches to |
|---|---|
| `.gt_state_*_*.json` (glob + json parse) | `SELECT * FROM engine_state` |
| `.gt_live_*_*.json` | `SELECT * FROM live_snapshots` |
| `data/audit/order_*_*.csv` (tail) | `SELECT * FROM orders WHERE ts > now() - interval '1 day' ORDER BY ts DESC LIMIT 20` |
| `data/alerts/alerts_*.jsonl` (tail) | `SELECT * FROM alerts ORDER BY ts DESC LIMIT 5` |
| `data/audit/state_*_*.csv` (tail) | `SELECT * FROM state_events WHERE symbol = $1 ORDER BY ts DESC LIMIT 6` |

### Refactor approach

Make the data source pluggable. Add `dashboard_agg/sources/` with `FileSource` (current) and `PgSource` (new). Aggregator picks based on `--source pg|file` flag or `GT_AGG_SOURCE` env var. Default to `file` so existing local workflow is unchanged.

---

## Migration Sequence (Post-Paper Week)

| Day | Task | Outcome |
|---|---|---|
| **Day 1 AM** | Provision RDS db.t4g.small in same VPC as EC2. Configure security group to allow inbound 5432 only from EC2's SG. Note connection string. | DB exists, accessible from EC2 only |
| **Day 1 AM** | Run schema DDL via psql. Create initial partitions for today + tomorrow for the high-volume tables. | All 7 tables exist, indexes ready |
| **Day 1 PM** | Write `pg_replicator.py`. Start with `engine_state` + `orders` + `alerts` (highest value, lowest volume). Test with a paper-trade replay. | Replicator daemon ships state + orders + alerts to PG |
| **Day 2 AM** | Add `live_snapshots` + `state_events` + `pnl_snapshots` to replicator. | All low-volume tables replicating |
| **Day 2 AM** | Set up systemd service for replicator. Add to bot's launch script. | Replicator auto-starts with bot, restarts on crash |
| **Day 2 PM** | (Optional) Add `feed_ticks` replication. Use `COPY` for bulk insert. Validate volume doesn't overwhelm db.t4g.small — if it does, drop to one-tick-per-second sampling for v1. | High-volume data replicating |
| **Day 3 AM** | Set up Grafana (~$0 self-hosted or ~$8/mo Cloud free tier) with PG datasource. Build first dashboard: equity curve, slippage histogram, latency over time, alert timeline. | Read-only analytics layer from your laptop browser |
| **Day 3 PM** | (Optional) Refactor `dashboard_agg.py` to support `--source pg`. | Aggregator works without SSH |
| **Day 4-5** | Build backtest harness that replays `feed_ticks` table through the live engine code to validate alternative trigger configurations. | Can A/B test strategy parameters against real session data |

**Total core work: 1.5 days. With optional aggregator + Grafana: 3 days.**

---

## Open Decisions (Resolve Before Starting)

| Question | Default I'd pick | Why |
|---|---|---|
| Managed RDS or self-hosted PG on the same EC2? | RDS | Separation of concerns; bot OOM doesn't kill DB |
| `db.t4g.small` or larger? | small | Volume well within capacity for 1-3 symbols |
| Replicate `feed_ticks` to PG in v1? | **No** | 1-2M rows/day is fine for db.t4g.small but doubles RDS storage cost and complicates schema; defer until you actually want to backtest |
| Use `psycopg[c]` (sync) or `asyncpg` (async)? | psycopg[c] | Replicator is standalone, doesn't share an event loop with anything; simpler |
| One replicator process per symbol, or one process for all? | one for all | Lower memory, no coordination between processes; sharded by file path |
| Partition retention: 30 / 90 / 365 days? | feed=30, others=365 | feed is huge, rest is small enough to keep a year |
| Grafana hosted or self-hosted? | Grafana Cloud free tier (1 user, 10 dashboards) | Zero ops, zero cost for one operator |
| `dashboard_agg.py` PG support — same process or separate `dashboard_agg_pg.py`? | Pluggable source, single file | Less duplication, easier maintenance |

---

## Checklist (to copy into the actual implementation PR)

### RDS setup
- [ ] Provision `db.t4g.small` in `us-east-1a` (same AZ as EC2 for lowest latency)
- [ ] Security group: inbound 5432 only from EC2's SG; no public access
- [ ] Enable automated backups (7-day retention)
- [ ] Create `gt_trading` database
- [ ] Create `gt_app` role with INSERT/UPDATE/SELECT on all tables (no DROP/ALTER)
- [ ] Store connection string in EC2 environment: `export GT_PG_DSN='postgresql://gt_app:...@<endpoint>:5432/gt_trading'`
- [ ] Test connection from EC2: `psql $GT_PG_DSN -c '\dt'`

### Schema
- [ ] Apply `schema/v1.sql` (DDL for all 7 tables + indexes + initial partitions)
- [ ] Add daily partition-creation cron / `pg_cron` job
- [ ] Add retention policy cron job

### Replicator
- [ ] `pg_replicator.py` — main loop with 200ms / 5s cadences
- [ ] Cursor persistence to `.pg_replicator_state.json`
- [ ] Exponential backoff retry on PG outage
- [ ] systemd unit file `pg_replicator.service`
- [ ] Test: kill replicator, restart, verify no duplicates and no gaps
- [ ] Test: stop PG, run replicator for 5 min, restart PG, verify catchup

### Validation
- [ ] Run paper bot + replicator for 1 hour, then verify row counts match audit CSV line counts
- [ ] Run query: `SELECT count(*), max(ts) FROM orders WHERE symbol = 'NVDA' AND date(ts) = current_date` — should match the day's audit CSV
- [ ] Run query: `SELECT * FROM engine_state` — should match current state file contents

### Aggregator (Phase 2)
- [ ] `dashboard_agg/sources/file_source.py` — extract existing logic
- [ ] `dashboard_agg/sources/pg_source.py` — new
- [ ] `--source pg|file` CLI flag, default `file`
- [ ] Test side-by-side: both sources produce identical summary view for same bot session

### Grafana (Phase 3, optional)
- [ ] Provision Grafana Cloud free account OR self-host on a tiny VPC instance
- [ ] Add PostgreSQL data source with read-only credentials
- [ ] Build 4 dashboards:
  - [ ] **Session overview** — equity curve, P&L histogram, trade timeline
  - [ ] **Slippage analysis** — per-fill slippage scatter, side-split avg, worst trades
  - [ ] **Latency** — pipeline p50/p99/max over time, order-placement latency
  - [ ] **Alerts feed** — table of all alerts with filter by severity + ticker

---

## What This Doesn't Solve

Worth being explicit so the next-week-version-of-us doesn't expect more than is on the tin:

- **Trading high availability.** If the EC2 bot dies, no Postgres replication makes the bot keep trading. Postgres just makes restart on a new machine faster. Real HA needs a second bot watching for the primary to fail.
- **Sub-millisecond latency.** Replicator doesn't help (or hurt) trade latency. EC2 colo + uvloop are the levers for that.
- **Strategy validation.** Postgres makes analysis easier but doesn't make the strategy better. That's the paper-test data's job.
- **Audit log tamper-evidence.** Files + PG are both rewritable. If you need legal-grade integrity, add per-row CRC or write to S3 with object-lock.

---

## References

- Current file-based audit: [src/config/audit.py](../src/config/audit.py) — `AuditManager` + 4 stream writers
- State persistence: [src/config/persistence.py](../src/config/persistence.py) — `StateStore` with fsync atomic writes
- Live snapshot writer: [run_live.py:448](../run_live.py:448) — `LiveTrader.write_live_snapshot()`
- Aggregator dashboard: [dashboard_agg.py](../dashboard_agg.py) — `SymbolWatcher` + `discover_watchers`
- Alert manager: [src/infra/alerts.py](../src/infra/alerts.py) — `AlertManager`, `FileChannel`, `SlackChannel`
- Reconcile + missed-fill replay: [src/strategy/engine.py:865-1020](../src/strategy/engine.py) — `_reconcile_missed_fills` + `_reconcile_position_state`
