"""RNG port — abstract randomness source.

WHY THIS EXISTS:
    Production code uses `random.uniform()` for paper-mode slippage
    simulation. That's a global module RNG — not reproducible without
    setting `random.seed()` globally (which is bad practice).

THE FIX:
    Inject a `RNG` Protocol implementation. Each test gets its own
    seeded RNG; same seed → identical "random" sequence. Different
    scenarios get different seeds (so they explore different paths
    of the system) but the same scenario always reproduces.

DESIGN NOTES:
    - Named sub-streams: an RNG can be split into named child RNGs
      (`rng.derive("paper_slippage")` vs `rng.derive("market_jitter")`).
      Same parent seed → same children → independent reproducible
      sub-streams. Crucial for debugging: changing the market simulator
      doesn't shift the paper-slippage seed.
    - Cryptographic strength is NOT needed — we want speed + reproducibility.
      numpy's PCG64 or Python's Mersenne Twister are both fine.
"""

from __future__ import annotations

import hashlib
import random
from typing import Protocol, runtime_checkable


@runtime_checkable
class RNG(Protocol):
    """Abstract randomness source. Engine + harness use ONLY this —
    never `random.uniform()` etc. directly."""

    @property
    def seed(self) -> int:
        """The seed this RNG was constructed from. Two RNGs with the
        same seed produce the same sequence of calls."""
        ...

    def uniform(self, lo: float, hi: float) -> float: ...
    def randint(self, lo: int, hi: int) -> int: ...
    def random(self) -> float: ...
    def choice(self, sequence: list) -> object: ...

    def derive(self, label: str) -> "RNG":
        """Return a CHILD RNG seeded deterministically from (self.seed, label).
        Independent stream — won't disturb the parent's sequence.

        Why this matters: tests want named sub-streams so changing one
        component doesn't shift the seed of another. Without it,
        adding one new `rng.uniform()` call to MockBroker would
        change the entire scenario's behavior.
        """
        ...


class DeterministicRNG:
    """Production-quality reproducible RNG. Default for the harness.

    Wraps random.Random (Mersenne Twister) with seed tracking and
    `derive()` for named sub-streams.
    """

    __slots__ = ('_seed', '_rng')

    def __init__(self, seed: int) -> None:
        self._seed: int = int(seed)
        self._rng = random.Random(self._seed)

    @property
    def seed(self) -> int:
        return self._seed

    def uniform(self, lo: float, hi: float) -> float:
        return self._rng.uniform(lo, hi)

    def randint(self, lo: int, hi: int) -> int:
        return self._rng.randint(lo, hi)

    def random(self) -> float:
        return self._rng.random()

    def choice(self, sequence: list) -> object:
        return self._rng.choice(sequence)

    def derive(self, label: str) -> "DeterministicRNG":
        # Derive a stable sub-seed from (parent_seed, label) via SHA-256.
        # Same (seed, label) → same child every time, but independent
        # of any siblings.
        digest = hashlib.sha256(
            f"{self._seed}/{label}".encode("utf-8")
        ).digest()
        # Take first 8 bytes → unsigned 64-bit int → Python random seed.
        sub_seed = int.from_bytes(digest[:8], byteorder="big", signed=False)
        return DeterministicRNG(sub_seed)


class NullRNG:
    """RNG that always returns the LOW bound. Useful for "no randomness"
    test cases where we want the most predictable path."""

    __slots__ = ('_seed',)

    def __init__(self, seed: int = 0) -> None:
        self._seed = seed

    @property
    def seed(self) -> int:
        return self._seed

    def uniform(self, lo: float, hi: float) -> float:
        return lo

    def randint(self, lo: int, hi: int) -> int:
        return lo

    def random(self) -> float:
        return 0.0

    def choice(self, sequence: list) -> object:
        if not sequence:
            raise IndexError("Cannot choose from empty sequence")
        return sequence[0]

    def derive(self, label: str) -> "NullRNG":
        return NullRNG(self._seed)


__all__ = ["RNG", "DeterministicRNG", "NullRNG"]
