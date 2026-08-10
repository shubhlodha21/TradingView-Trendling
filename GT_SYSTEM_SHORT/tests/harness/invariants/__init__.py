"""Invariant library — registers all concrete invariants into REGISTRY on import.

Each module under invariants/ exposes a `register_all()` function.
We call them all here so a single `from tests.harness.invariants import *`
populates the registry.
"""

from .core import (
    PriceOnVenueGridInvariant, ModifyNotReplaceInvariant,
    PositionQtyMatchInvariant, NoOffGridOrdersInvariant,
    SellStopBelowEntryInvariant,
    register_all as _register_core,
)

# Register at module import time.
_register_core()

__all__ = [
    "PriceOnVenueGridInvariant", "ModifyNotReplaceInvariant",
    "PositionQtyMatchInvariant", "NoOffGridOrdersInvariant",
    "SellStopBelowEntryInvariant",
]
