# GT System - Claude Memory

## Current System State (Pre-Order-Management-Upgrade)

### Branch: nabi
### Last Working Commit: cb669dc (signal handlers fix)

### Backup Status: SAVED

Before implementing Stop-Limit order management (SL-LIMIT), the system was fully functional with:
- Market orders for entry/exit
- Paper trading with slippage simulation
- Breakout re-entry logic
- Full audit logging (feed, state, orders, pnl)
- Terminal command shortcuts

---

## Order Management Upgrade Plan (Pending Implementation)

### Senior's Requirements

1. **Entry (BUY)**: Optional Market OR Limit @ trigger price
   - Lowest latency possible
   - LTP >= trigger → place order

2. **Exit (SELL)**: STOP-LIMIT order
   - Trigger: 5-10 cents above entry/current
   - Limit: trigger - spread
   - Minimizes slippage

3. **Re-Entry**: LIMIT @ previous cycle's high
   - Then SL-LIMIT protection

### Implementation Notes

- Keep current system functional
- Add order management as new layer
- Test in paper mode first
- IBKR supports: Market, Limit, Stop, Stop-Limit

---

## Bug Recovery

If order management upgrade causes issues:
1. `git checkout cb669dc` to revert to working state
2. Check backup notes in this file
3. Core files to restore:
   - `src/strategy/engine.py`
   - `src/execution/broker.py`
   - `run_live.py`

---

## Recent Commits (nabi branch)

| Commit | Description |
|--------|-------------|
| cb669dc | Fix signal handlers for Linux compatibility |
| 1c34152 | Add terminal command shortcuts |
| 21d2518 | Increase max consecutive losses 3→300 |
| 95c23c4 | Fix order history tracking |
| 747c4d0 | Show more orders in dashboard |
| 40414d5 | Comprehensive audit logging |

---

## Key Files Reference

- Entry logic: `src/strategy/engine.py` → `_enter()`
- Exit logic: `src/strategy/engine.py` → `_exit()`
- Order execution: `src/execution/broker.py` → `place_order()`
- Dashboard: `dashboard.py`

---

## System Behavior Summary

1. **Entry Flow**:
   - LTP crosses trigger → BUY SUBMITTED → FILLED → IN_POSITION
   - Stop set to entry * (1 - stop_pct)

2. **Exit Flow**:
   - LTP <= stop → SELL SUBMITTED → FILLED → WAITING_REENTRY
   - previous_breakout_level = highest price

3. **Re-Entry Flow**:
   - LTP crosses previous_breakout_level → BUY again

4. **State Machine**:
   - IDLE → MONITORING → ORDER_ENTRY → IN_POSITION → EXIT_POSITION → WAITING_REENTRY → MONITORING

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
| ------ | ---------- |
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
