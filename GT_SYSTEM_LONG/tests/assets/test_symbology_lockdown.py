"""Architectural lockdown for instrument symbology.

WHY THIS TEST EXISTS
====================
IBKR stores Forex contracts with `secType="CASH", symbol=<base_ccy>,
currency=<quote_ccy>` — so `Forex("EURUSD").symbol == "EUR"`, NOT
"EURUSD". Equity, CFD, and Future contracts keep the logical ticker
on `contract.symbol`, but FX does not.

Every place in the codebase that compares broker contract data against
a logical ticker (like `config.ticker == "EURUSD"`) MUST go through
`_logical_symbol_from_contract()` — bare `contract.symbol` comparisons
silently break for every FX pair, leading to phantom POSITION_MISMATCH
folds that kill protective stops on real positions (live regression
2026-06-05).

This test enforces the pattern at the SOURCE level by grepping every
production module for bare `contract.symbol == X` (or `!= X`) and
failing if any are found outside the allow-listed translation layer.
Tests + scripts/ are exempt — they're not on the live trading path.

If you DELIBERATELY need to compare on the raw broker symbol (e.g.
inside a contract policy's `identify()` method), the allow-list at
the bottom of this file lets you opt in explicitly with a one-line
justification — keeps the audit trail intact.

THE PATTERN TO FOLLOW
=====================
- Need to filter positions/orders/fills by your ticker?
    Iterate via `gateway.get_positions()` / `gateway.fetch_open_orders()`
    — both return logical tickers in the `symbol` field.

- Need to translate an arbitrary ib_async contract yourself?
    Call `_logical_symbol_from_contract(contract)` — handles every
    asset class via the contract policies' `identify()` methods.

- Need to handle a brand-new asset class?
    Add an `identify(ib_contract) -> Optional[str]` staticmethod to
    your new contract policy and register it in
    `_logical_symbol_from_contract`. The lockdown test will then start
    covering you for free.
"""

import re
from pathlib import Path

import pytest


# Production code roots — every .py under these is in scope.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROD_DIRS = ("src",)
_PROD_FILES = ("run_live.py", "dashboard.py")

# Files where bare-symbol comparison is LEGITIMATE. Each entry must
# carry a one-line justification — when reviewing a PR that adds to
# this list, the justification is the audit trail.
_ALLOWLIST = {
    # src/execution/broker.py is the symbology boundary — it IS the
    # translation layer, so reading raw contract fields here is the
    # whole point of the module.
    "src/execution/broker.py":
        "broker.py owns _logical_symbol_from_contract; raw reads are intentional.",
    # The contract policies' identify() methods MUST inspect raw
    # secType / symbol / currency to do their job.
    "src/assets/forex.py":
        "IDEALPROForexContract.identify() reads raw secType/symbol/currency.",
    "src/assets/us_stock.py":
        "SMARTStockContract.identify() reads raw secType/symbol.",
    "src/assets/cfds.py":
        "CFDContract.identify() reads raw secType/symbol.",
    "src/assets/future.py":
        "FuturesContractPolicy.identify() reads raw secType/symbol.",
}

# Pattern: any `<expr>.contract.symbol <op> <expr>` where op is == / !=.
# Captures comparisons in either direction (`x.contract.symbol == y` OR
# `y == x.contract.symbol`). Also catches `fill.contract.symbol`,
# `trade.contract.symbol`, `pos.contract.symbol`, etc.
_BAD_PATTERNS = [
    re.compile(r"\.contract\.symbol\s*(==|!=)"),
    re.compile(r"(==|!=)\s*[a-zA-Z_][\w\.]*\.contract\.symbol"),
]


def _iter_prod_files() -> list[Path]:
    files: list[Path] = []
    for d in _PROD_DIRS:
        for p in (_REPO_ROOT / d).rglob("*.py"):
            files.append(p)
    for f in _PROD_FILES:
        p = _REPO_ROOT / f
        if p.exists():
            files.append(p)
    return files


def test_no_bare_contract_symbol_comparisons_in_production():
    """Fail if any production file compares `contract.symbol` directly
    against a value (instead of routing through
    `_logical_symbol_from_contract()`).

    This is the lockdown that prevents the FX-vs-equity symbol mismatch
    bug class from returning. If this test fails, you almost certainly
    have a regression that will silently corrupt FX trading.
    """
    violations: list[tuple[str, int, str]] = []
    for path in _iter_prod_files():
        rel = str(path.relative_to(_REPO_ROOT))
        if rel in _ALLOWLIST:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.split("#", 1)[0]  # ignore comments
            for pat in _BAD_PATTERNS:
                if pat.search(stripped):
                    violations.append((rel, lineno, line.rstrip()))
                    break

    if violations:
        body = "\n".join(
            f"  {rel}:{ln}\n      {code}"
            for rel, ln, code in violations
        )
        pytest.fail(
            "SYMBOLOGY LOCKDOWN VIOLATION\n"
            "============================\n"
            "Found bare `contract.symbol` comparisons in production code. "
            "These break silently for FX (ib_async stores Forex('EURUSD') "
            "as contract.symbol='EUR'). Route through "
            "`_logical_symbol_from_contract()` in src/execution/broker.py, "
            "OR iterate via `gateway.get_positions()` / "
            "`gateway.fetch_open_orders()` which already translate.\n\n"
            f"Violations:\n{body}\n\n"
            "If a comparison is GENUINELY intentional (e.g. inside a new "
            "contract policy's identify() method), add the file to "
            "_ALLOWLIST in this test with a one-line justification."
        )


def test_all_contract_policies_implement_identify():
    """Every contract policy MUST implement `identify(ib_contract) ->
    Optional[str]`. Without it, `_logical_symbol_from_contract` will
    fall through to the raw broker symbol — silently breaking the
    asset class when run live.

    When you add a new asset class (options, bonds, crypto, etc.),
    this test will fail until you add `identify()` to its contract
    policy. That's by design — it's the trigger to add the policy to
    `_logical_symbol_from_contract`'s walked-policies list too.
    """
    from src.assets.cfds import CFDContract
    from src.assets.forex import IDEALPROForexContract
    from src.assets.future import FuturesContractPolicy
    from src.assets.us_stock import SMARTStockContract

    for policy in (
        SMARTStockContract,
        IDEALPROForexContract,
        CFDContract,
        FuturesContractPolicy,
    ):
        assert hasattr(policy, "identify"), (
            f"{policy.__name__} missing identify() — bare contract symbols "
            f"from this asset class will leak into engine code."
        )
        # Smoke check: passing a None-like contract returns None
        # (i.e. the policy doesn't crash on garbage input).
        try:
            result = policy.identify(type("Fake", (), {})())
        except Exception as e:
            pytest.fail(
                f"{policy.__name__}.identify() crashed on synthetic empty "
                f"contract: {type(e).__name__}: {e}"
            )
        assert result is None or isinstance(result, str), (
            f"{policy.__name__}.identify() returned {type(result).__name__} "
            f"— must be Optional[str]."
        )


def test_logical_symbol_from_contract_handles_every_asset_class():
    """End-to-end check that the centralized translator handles every
    asset class correctly. If you add a new asset class and forget to
    register its contract policy in `_logical_symbol_from_contract`,
    this test will fail on the new case.
    """
    from src.execution.broker import _logical_symbol_from_contract

    class _Fake:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    cases = [
        # (label, contract, expected_logical_ticker)
        ("FX EURUSD",     _Fake(secType="CASH", symbol="EUR", currency="USD"),  "EURUSD"),
        ("FX USDJPY",     _Fake(secType="CASH", symbol="USD", currency="JPY"),  "USDJPY"),
        ("FX GBPCHF",     _Fake(secType="CASH", symbol="GBP", currency="CHF"),  "GBPCHF"),
        ("Equity AAPL",   _Fake(secType="STK",  symbol="AAPL", currency="USD"), "AAPL"),
        ("Equity SPY",    _Fake(secType="STK",  symbol="SPY",  currency="USD"), "SPY"),
        ("Future ES",     _Fake(secType="FUT",  symbol="ES",   currency="USD"), "ES"),
        ("Future MES",    _Fake(secType="FUT",  symbol="MES",  currency="USD"), "MES"),
        ("ContFuture",    _Fake(secType="CONTFUT", symbol="GC", currency="USD"), "GC"),
        ("CFD IBUS500",   _Fake(secType="CFD",  symbol="IBUS500", currency="USD"), "IBUS500"),
        # Unknown asset class — falls through to raw symbol (forward-compat).
        ("OPT passthrough", _Fake(secType="OPT", symbol="SPY",  currency="USD"), "SPY"),
    ]

    for label, contract, expected in cases:
        got = _logical_symbol_from_contract(contract)
        assert got == expected, (
            f"{label}: _logical_symbol_from_contract returned {got!r}, "
            f"expected {expected!r}. If this is a new asset class, add its "
            f"contract policy to _logical_symbol_from_contract's walked list."
        )
