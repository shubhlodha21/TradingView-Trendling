# GT Feed System - Complete Documentation

## Overview

The GT Feed System is a high-performance market data pipeline built for mid-frequency trading. It processes tick data through a series of validation and normalization stages before passing to the trading engine.

**Performance:**
- 239,000+ ticks/second throughput
- Sub-millisecond pipeline processing
- Designed for <2ms end-to-end latency

---

## Quick Start

```bash
# Basic usage - fetch AAPL data
python3 test_feed.py

# Fetch specific symbol
python3 test_feed.py -s NVDA

# Multiple days with different bar size
python3 test_feed.py -s TSLA -d "5 D" -b "5 min"
```

---

## Command Line Options

### Required/Common Options

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--symbol` | `-s` | `AAPL` | Trading symbol to fetch |
| `--duration` | `-d` | `1 D` | Historical data duration |
| `--bar-size` | `-b` | `1 min` | Bar aggregation size |

### Connection Options

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `127.0.0.1` | IBKR Gateway host address |
| `--port` | `4001` | IBKR Gateway port |
| `--client-id` | `1` | Client ID for this connection |

---

## Duration Format

| Format | Example | Description |
|--------|---------|-------------|
| `X D` | `1 D` | X days of data |
| `X W` | `2 W` | X weeks of data |
| `X M` | `1 M` | X months of data |
| `X Y` | `1 Y` | X years of data |

**Examples:**
```bash
python3 test_feed.py -d "1 D"    # 1 day
python3 test_feed.py -d "5 D"    # 5 days
python3 test_feed.py -d "2 W"    # 2 weeks
python3 test_feed.py -d "1 M"    # 1 month
```

---

## Bar Size Format

| Format | Example | Description |
|--------|---------|-------------|
| `X secs` | `5 secs` | X second bars |
| `X mins` | `1 min` | X minute bars |
| `X hours` | `1 hour` | X hour bars |
| `X days` | `1 day` | X day bars |

**Valid Bar Sizes:**
```bash
# Seconds
python3 test_feed.py -b "5 secs"
python3 test_feed.py -b "15 secs"
python3 test_feed.py -b "30 secs"

# Minutes
python3 test_feed.py -b "1 min"
python3 test_feed.py -b "5 mins"
python3 test_feed.py -b "15 mins"
python3 test_feed.py -b "30 mins"

# Hours
python3 test_feed.py -b "1 hour"
python3 test_feed.py -b "2 hours"
python3 test_feed.py -b "4 hours"

# Days
python3 test_feed.py -b "1 day"
python3 test_feed.py -b "1 week"
```

---

## Common Use Cases

### Daily Trading - 1 Minute Bars
```bash
python3 test_feed.py -s AAPL -d "1 D" -b "1 min"
```

### Swing Trading - Hourly Bars
```bash
python3 test_feed.py -s SPY -d "5 D" -b "1 hour"
```

### Weekly Analysis - Daily Bars
```bash
python3 test_feed.py -s QQQ -d "3 M" -b "1 day"
```

### Momentum Trading - 5 Minute Bars
```bash
python3 test_feed.py -s TSLA -d "2 W" -b "5 mins"
```

### Multiple Clients
```bash
# Terminal 1
python3 test_feed.py -s AAPL --port 4001 --client-id 1

# Terminal 2
python3 test_feed.py -s TSLA --port 4002 --client-id 2
```

---

## Pipeline Stages

The data flows through these validation stages:

### 1. SequenceMonitor
- Detects out-of-order ticks
- Identifies gaps in data stream
- Tracks sequence numbers per symbol

### 2. Deduplicator
- Removes duplicate ticks
- Configurable dedup window (default: 1 second)
- Memory-efficient with bounded cache

### 3. Normalizer
- Standardizes tick format
- Rounds prices to configurable precision
- Normalizes timestamps to UTC

### 4. Validator
- Price bounds checking
- Bid/ask consistency
- Timestamp validation
- Rate-of-change limits

---

## Performance Characteristics

### Measured Throughput
```
Pipeline:     239,000 ticks/sec
Per tick:    0.004 ms
Memory:      Uses __slots__ for all dataclasses
Allocations: Minimal per-tick (reuse patterns where possible)
```

### Latency Budget (Target: <2ms)
```
Tick received:              0.00 ms
  └─ Pipeline processing:  0.00 ms
  └─ Strategy engine:      ~0.50 ms
  └─ Gateway + API:        ~1.00 ms
                          --------
  Total end-to-end:        ~1.50 ms
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        IBKR Gateway                          │
│                    (127.0.0.1:4001)                         │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    ConnectionManager                          │
│  - Manages IBKR connection                                   │
│  - Handles reconnection                                     │
│  - Emits heartbeat events                                   │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      Pipeline Chain                           │
│                                                              │
│  Tick ──► SequenceMonitor ──► Deduplicator ──►              │
│              Normalizer ──► Validator ──► Tick               │
│                                                              │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    CandleBuilder                             │
│  - Aggregates ticks into OHLCV bars                         │
│  - Multi-timeframe support (1m, 5m, 15m, 1h, 1d)           │
│  - Emits bar completion events                               │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Strategy Engine                            │
│  - Entry/exit signals                                       │
│  - Risk management                                          │
│  - Order execution                                          │
└─────────────────────────────────────────────────────────────┘
```

---

## Symbols

The system supports any US equity available on IBKR:

### Tech Stocks
```bash
python3 test_feed.py -s AAPL   # Apple
python3 test_feed.py -s MSFT   # Microsoft
python3 test_feed.py -s GOOGL  # Google
python3 test_feed.py -s AMZN   # Amazon
python3 test_feed.py -s NVDA   # NVIDIA
python3 test_feed.py -s META   # Meta
python3 test_feed.py -s TSLA   # Tesla
```

### ETFs
```bash
python3 test_feed.py -s SPY    # S&P 500
python3 test_feed.py -s QQQ    # NASDAQ 100
python3 test_feed.py -s IWM    # Russell 2000
python3 test_feed.py -s TLT    # 20+ Year Treasury
```

### Indian Stocks (via SMART routing)
```bash
python3 test_feed.py -s INFY.NSE   # Infosys
python3 test_feed.py -s TCS.NSE     # Tata Consultancy
python3 test_feed.py -s RELIANCE.BSE  # Reliance
```

---

## Data Modes

### Delayed Data (Free)
IBKR provides 15-minute delayed market data for free. This is the default mode.

```bash
# Uses delayed data (no subscription needed)
python3 test_feed.py
```

### Live Data (Requires Subscription)
To get real-time data, subscribe to IBKR's market data package:

1. Log into Account Management
2. Go to Market Data Subscriptions
3. Subscribe to desired exchanges

---

## Output Format

### OHLCV Display
```
  TIME       OPEN      HIGH       LOW     CLOSE    CHANGE     VOL (K)
───────────────────────────────────────────────────────────────────────
  09:30   134.81    136.09    134.79   136.07         -       10K
  09:31   136.10    136.66    135.35   135.44   ▼ -0.63        2K
  09:32   135.46    135.55    134.64   134.97   ▼ -0.47        2K
```

### Change Indicators
- `▲ +X.XX` - Price increased (green)
- `▼ -X.XX` - Price decreased (red)
- `  X.XX` - No change (white)

### Volume Formatting
- `< 1000` - Raw number (e.g., `803`)
- `1K - 999K` - Thousands (e.g., `10K`)
- `1M+` - Millions (e.g., `1.5M`)

---

## Troubleshooting

### Connection Issues

**Error: "Connection failed"**
```bash
# Check if IBKR Gateway is running
# Default port is 4001, try alternative:
python3 test_feed.py --port 4002
```

**Error: "Contract not found"**
```bash
# Verify symbol is correct
python3 test_feed.py -s AAPL
# Try with explicit exchange
python3 test_feed.py -s INFY.NSE
```

### Data Issues

**No bars returned**
```bash
# Market may be closed, try different duration
python3 test_feed.py -d "2 D" -b "1 hour"
```

**Stale data warning**
```bash
# Check data mode - delayed vs live
# IBKR delayed data has 15-min lag on free tier
```

---

## File Structure

```
gt_system/
├── docs/
│   └── FEED_SYSTEM.md          # This documentation
├── src/
│   ├── feed/
│   │   ├── __init__.py
│   │   ├── cache.py             # Market data cache
│   │   ├── connection.py        # IBKR connection manager
│   │   ├── handler.py           # Tick data models
│   │   ├── candles/
│   │   │   └── builder.py       # OHLCV bar builder
│   │   └── pipeline/
│   │       ├── base.py          # Pipeline stage base
│   │       ├── chain.py         # Pipeline chain
│   │       ├── deduplicator.py  # Deduplication
│   │       ├── normalizer.py    # Data normalization
│   │       ├── sequence.py      # Sequence monitoring
│   │       └── validator.py     # Data validation
│   ├── strategy/
│   │   ├── engine.py            # Trading engine
│   │   ├── logging.py           # Structured logging
│   │   └── risk.py              # Risk management
│   └── config/
│       ├── models.py            # Data models
│       └── loader.py            # Config loader
└── test_feed.py                  # CLI tool
```

---

## API Reference

### ConnectionManager

```python
from src.feed.connection import ConnectionManager, ConnectionConfig

config = ConnectionConfig(host="127.0.0.1", port=4001, client_id=1)
conn = ConnectionManager(config)
await conn.connect()
# ... use connection ...
await conn.disconnect()
```

### PipelineChain

```python
from src.feed.pipeline.base import PipelineChain
from src.feed.pipeline.sequence import SequenceMonitor
from src.feed.pipeline.deduplicator import Deduplicator
from src.feed.pipeline.normalizer import Normalizer
from src.feed.pipeline.validator import Validator

pipeline = PipelineChain()
pipeline.add_stage(SequenceMonitor())
pipeline.add_stage(Deduplicator())
pipeline.add_stage(Normalizer())
pipeline.add_stage(Validator())

result = pipeline.process(tick)
```

### CandleBuilder

```python
from src.feed.candles.builder import CandleBuilder

builder = CandleBuilder(symbol="AAPL", timeframes=["1m", "5m", "15m"])

# Subscribe to bar completion
builder.on_bar("5m", lambda bar: print(f"5m close: {bar.close}"))

# Add tick
builder.add_tick(tick)

# Get completed bars
bars = builder.get_bars("5m", count=100)
```

---

## License

Internal use only. Built for the GT Trading System.
