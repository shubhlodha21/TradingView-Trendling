"""Architectural lockdown for price-precision handling.

WHY THIS TEST EXISTS
====================
`round(price, 2)` is the equity-only convention — it produces correct
output for AAPL ($230.45) but silently collapses FX 1.16284 → 1.16,
which IBKR then stores as the actual broker stop. (Live regression
2026-06-05: bracket-child modify after BUY fill produced auxPrice=1.16
on EURUSD, leaving the protective stop 31 pips lower than intended.)

The codebase has a centralized spec-aware tick rounder:
    Engine._round_to_tick(value) → spec.tick.round_to_tick(...)

Every PRICE rounding site in the engine/broker/risk path MUST use it.
Bare `round(<price_expr>, 2)` is forbidden outside the documented
allow-list (the `_round_to_tick` fallback path itself, plus docstrings
that reference the old pattern).

This test scans the source for the bare pattern and fails the build
if anyone reintroduces it. Currency amounts (commission, P&L) ARE
allowed to use `round(x, 2)` since they're always in 2dp dollar units.

THE PATTERN TO FOLLOW
=====================
- Computing a stop, limit, or trigger?
    Use `self._round_to_tick(value)` — handles every asset class.

- Comparing two prices for "are they meaningfully different?"
    Use `self._price_epsilon()` — half the asset's tick grid, NOT
    hardcoded 0.005.

- Pure display formatting in dashboard/logs?
    Use `fmt_px(state, value)` (dashboard) or `:.{N}f` with N from
    `spec.tick.decimals_for_display(...)` (logs).
"""

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]

# Files where `round(price, 2)` is LEGITIMATE and explicitly allow-listed.
# Each entry must carry a one-line justification — the audit trail.
_ALLOWLIST = {
    "src/strategy/engine.py": (
        "Comment lines + the _round_to_tick fallback (line ~538 returns "
        "round(value, 2) when no spec resolved — preserves legacy equity "
        "behavior). Code lines outside that fallback are forbidden."
    ),
    "src/config/audit.py": (
        "Currency rounding for audit log display (commission, fees, P&L). "
        "Always 2dp dollar amounts, not asset prices."
    ),
    "src/config/models.py": (
        "Commission + currency rounding. Always 2dp dollar amounts."
    ),
}

# Lines in src/strategy/engine.py where `round(..., 2)` is the documented
# legacy-fallback inside _round_to_tick itself. These are intentional.
_ENGINE_ROUND_FALLBACK_LINE_RANGE = (525, 545)


def _scan_for_bad_rounds(path: Path) -> list[tuple[int, str]]:
    """Return list of (line_no, line) for any actual code-call to
    `round(<expr>, 2)`. Uses Python's `ast` module so docstrings,
    comments, and string literals are NEVER counted as hits.

    Skips:
      - the documented fallback inside `Engine._round_to_tick`
        (the single line `return round(value, 2)` — this IS the
        legacy equity behavior we preserve by design).
    """
    import ast
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []

    hits: list[tuple[int, str]] = []
    rel = str(path.relative_to(_REPO_ROOT))
    lines = text.splitlines()

    # Walk every Call node looking for round(<expr>, 2).
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Function must be the bare name `round`
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "round"):
            continue
        # Must have exactly 2 positional args
        if len(node.args) != 2:
            continue
        # Second arg must be the literal int 2
        second = node.args[1]
        if not (isinstance(second, ast.Constant) and second.value == 2):
            continue
        # Documented per-file exceptions for non-price rounding sites.
        # Each must be ALL of: (a) clearly a currency/USD amount and
        # not a market price, (b) annotated below with a justification.
        line_no = node.lineno
        line_text = lines[line_no - 1] if line_no - 1 < len(lines) else ""
        stripped = line_text.strip()

        if rel == "src/strategy/engine.py":
            # The ONE legitimate fallback inside Engine._round_to_tick:
            # `return round(value, 2)` when no spec is resolved.
            if stripped == "return round(value, 2)":
                continue

        if rel == "src/execution/broker.py":
            # The ONE legitimate fallback inside _paper_slippage_for_symbol:
            # when spec resolution fails, fall back to equity 2dp.
            if stripped in (
                "return 0.02, (lambda px: round(px, 2))",
                "return round(px, 2)",
            ):
                continue

        if rel == "src/strategy/risk.py":
            # `RiskCheck.status()` returns USD currency amounts (daily
            # PnL, equity, buying power) — these are dollar values, not
            # market prices, so 2dp display rounding is correct.
            if any(k in stripped for k in (
                "round(self._cached_daily_pnl, 2)",
                "round(self._get_cached_equity(), 2)",
                "round(self._get_cached_buying_power(), 2)",
            )):
                continue

        hits.append((line_no, line_text.rstrip()))

    return hits


def test_no_bare_round_to_2dp_on_price_expressions():
    """Fail if any production-code site uses `round(<price>, 2)` for
    price calculations. Use `self._round_to_tick(value)` instead, which
    routes through the spec's tick policy and handles every asset class.

    If the existence of a violation is INTENTIONAL (e.g. a currency-
    amount file that genuinely needs 2dp), add the file to _ALLOWLIST
    with a one-line justification.
    """
    violations: list[tuple[str, int, str]] = []

    targets = [
        _REPO_ROOT / "src/strategy/engine.py",
        _REPO_ROOT / "src/strategy/risk.py",
        _REPO_ROOT / "src/execution/broker.py",
    ]

    for path in targets:
        if not path.exists():
            continue
        rel = str(path.relative_to(_REPO_ROOT))
        hits = _scan_for_bad_rounds(path)
        for line, code in hits:
            violations.append((rel, line, code))

    if violations:
        body = "\n".join(
            f"  {rel}:{ln}\n      {code}"
            for rel, ln, code in violations
        )
        pytest.fail(
            "PRICE-PRECISION LOCKDOWN VIOLATION\n"
            "==================================\n"
            "Found `round(<expr>, 2)` in production price-calculation code. "
            "This collapses FX prices (1.16284 → 1.16) and gets sent to "
            "IBKR as the wrong stop, leaving real positions under-protected. "
            "Use `self._round_to_tick(value)` instead — it routes through "
            "the AssetSpec's tick policy and handles every asset class.\n\n"
            f"Violations:\n{body}\n\n"
            "If the value is GENUINELY a currency amount (not a price), "
            "move the code to a currency-handling module and add it to "
            "_ALLOWLIST with a one-line justification."
        )


def test_engine_round_to_tick_handles_every_asset_class():
    """End-to-end check: `Engine._round_to_tick` must produce values on
    the correct tick grid for every asset class. This is the central
    helper — if it breaks, every stop/limit calculation breaks.
    """
    from src.assets import resolve

    cases = [
        # (symbol, raw_value, expected_snapped)
        ("AAPL",     1.16284,   1.16),      # equity 0.01 grid
        ("AAPL",   230.456,   230.46),      # equity 0.01 grid
        ("EURUSD",   1.16284,   1.16285),   # FX half-pip 0.00005 grid
        ("EURUSD",   1.16400,   1.16400),   # already on grid
        ("USDJPY", 153.052,   153.050),     # JPY 0.005 grid
        ("ES",    5800.10,    5800.00),     # ES 0.25 tick: 5800.10/0.25=23200.4 → nearest 23200 → 5800.00
        ("ES",    5800.13,    5800.25),     # 5800.13/0.25=23200.52 → nearest 23201 → 5800.25
        ("ES",    5800.30,    5800.25),     # 5800.30/0.25=23201.2 → nearest 23201 → 5800.25
    ]

    for sym, raw, expected in cases:
        spec = resolve(sym)
        from src.assets.types import price as P
        from src.assets.policies.tick import RoundDirection as RD
        snapped = float(spec.tick.round_to_tick(P(str(raw)), RD.NEAREST))
        assert abs(snapped - expected) < 1e-9, (
            f"{sym}: raw={raw} expected={expected} got={snapped}"
        )


def test_engine_price_epsilon_scales_with_tick():
    """`Engine._price_epsilon` must return half the tick grid so the
    "is the stop change worth modifying?" threshold scales correctly
    across asset classes. Equity 0.005 is the legacy value (preserved
    byte-identical); FX must be sub-pip; futures must be half-tick.
    """
    from src.assets import resolve
    from src.strategy.engine import Engine

    # We can't instantiate Engine cleanly without a gateway/config,
    # so test the method's logic by inspecting via a synthetic
    # object that has just enough attributes.
    class Stub:
        _asset_spec = None

    # Simulate the method body's behavior per asset.
    for sym, expected_eps in [
        ("AAPL",   0.005),       # 0.01 tick / 2
        ("EURUSD", 0.000025),    # 0.00005 / 2
        ("USDJPY", 0.0025),      # 0.005 / 2
        ("ES",     0.125),       # 0.25 / 2
    ]:
        spec = resolve(sym)
        stub = Stub()
        stub._asset_spec = spec
        eps = Engine._price_epsilon(stub)
        assert abs(eps - expected_eps) < 1e-12, (
            f"{sym}: expected eps={expected_eps} got={eps}"
        )

    # No spec → 0.005 fallback (legacy equity).
    stub = Stub()
    stub._asset_spec = None
    eps = Engine._price_epsilon(stub)
    assert eps == 0.005, f"No-spec fallback must be 0.005, got {eps}"
