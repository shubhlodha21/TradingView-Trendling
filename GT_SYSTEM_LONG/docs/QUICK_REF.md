# GT Feed System - Quick Reference Card

## Common Commands

```bash
# Default (AAPL, 1 day, 1 min bars)
python3 test_feed.py

# Fetch specific symbol
-s, --symbol     Example: -s NVDA

# Duration
-d, --duration  1 D, 5 D, 2 W, 1 M

# Bar size
-b, --bar-size   1 min, 5 mins, 1 hour

# Connection
--host           127.0.0.1 (default)
--port           4001 (default)
--client-id      1 (default)
```

## Duration Chart

| Format | Example |
|--------|---------|
| `X D`  | `1 D`, `5 D`, `30 D` |
| `X W`  | `1 W`, `2 W`, `4 W` |
| `X M`  | `1 M`, `3 M`, `12 M` |

## Bar Size Chart

| Format | Valid Examples |
|--------|---------------|
| secs   | `5 secs`, `15 secs`, `30 secs` |
| mins   | `1 min`, `5 mins`, `15 mins`, `30 mins` |
| hours  | `1 hour`, `2 hours`, `4 hours` |
| days   | `1 day`, `1 week` |

## Examples

```bash
# Day trading setup
python3 test_feed.py -s AAPL -d "1 D" -b "1 min"

# Swing trading
python3 test_feed.py -s TSLA -d "5 D" -b "15 mins"

# Weekly analysis
python3 test_feed.py -s SPY -d "2 W" -b "1 hour"

# Monthly review
python3 test_feed.py -s QQQ -d "3 M" -b "1 day"

# Multiple clients
python3 test_feed.py -s AAPL --port 4001 --client-id 1 &
python3 test_feed.py -s TSLA --port 4002 --client-id 2 &
```

## Help

```bash
python3 test_feed.py --help
```

## Performance

- Throughput: 239,000 ticks/sec
- Per tick: 0.004 ms
- Target latency: <2ms end-to-end

## Pipeline Stages

1. SequenceMonitor - Out-of-order detection
2. Deduplicator - Remove duplicates
3. Normalizer - Standardize format
4. Validator - Data quality checks