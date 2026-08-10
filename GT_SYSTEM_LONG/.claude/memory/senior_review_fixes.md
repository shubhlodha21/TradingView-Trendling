---
name: senior_review_fixes_applied
description: Applied critical fixes from senior quant review - paper trading race condition, stale price exit, consecutive losses logic, market hours check, heartbeat, state machine transitions
type: project
---

## Critical Fixes Applied from Senior Review (2026-05-08)

### 1. Paper Trading Race Condition (HIGH)
- **File**: src/execution/broker.py
- **Issue**: _simulate_paper_fill fired async but order not tracked yet
- **Fix**: Fire fill SYNCHRONOUSLY in paper mode, order tracked before place_order call

### 2. Paper Trading Fill Price (HIGH)
- **File**: src/execution/broker.py
- **Issue**: Used hardcoded price=100.0 regardless of market
- **Fix**: Added _get_current_market_price() to fetch real market data

### 3. Stale Price Exit Bug (CRITICAL)
- **File**: src/strategy/engine.py
- **Issue**: _exit_position used potentially stale current_price
- **Fix**: Added staleness check (30s), fetches fresh price if stale, fallback to stop_loss_price

### 4. Consecutive Losses Logic (HIGH)
- **File**: src/strategy/risk_manager.py
- **Issue**: pnl=0 (flat) was resetting consecutive losses
- **Fix**: Changed condition from `else:` to `elif pnl > Decimal("0")`

### 5. Hardcoded Capital (HIGH)
- **File**: src/strategy/engine.py
- **Issue**: Pre-trade check used hardcoded 100000
- **Fix**: Query gateway.get_account_info() for real capital, use 50% for margin safety

### 6. Market Hours Check (MEDIUM)
- **File**: src/strategy/engine.py
- **Issue**: check_market_hours() existed but never called
- **Fix**: Added market hours check before entry in _process_monitoring

### 7. Heartbeat Not Updated (MEDIUM)
- **File**: src/execution/broker.py
- **Issue**: _update_heartbeat() existed but never called
- **Fix**: Call _update_heartbeat on data receipt, connect, and get_current_price

### 8. State Machine Transition Duplicates (HIGH)
- **File**: src/strategy/states/trade_state.py
- **Issue**: EMERGENCY_STOP added as separate entries, overwriting system flow transitions
- **Fix**: Consolidated TRANSITIONS dict - each state has single entry with all valid transitions

### 9. Silent Transition Failures (MEDIUM)
- **File**: src/strategy/engine.py
- **Issue**: transition_to returning False silently ignored
- **Fix**: Added logging warning when transition fails

### Test Status: 176 tests passing