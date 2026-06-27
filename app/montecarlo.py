"""Monte Carlo π estimation — the REAL, unit-tested compute at the heart of the
demo. No mocking: workers actually run this loop on real CPU nodes.

The method: throw random darts at the unit square [0,1)x[0,1). The fraction that
land inside the quarter unit circle (x^2 + y^2 <= 1) approaches π/4 as the number
of darts grows, so π ≈ 4 * inside / total. It is *embarrassingly parallel* — each
worker samples an independent stream and reports its (inside, total) partial; the
leader sums the partials across all workers into one running estimate.

This module is pure stdlib (the ``random`` module) so it runs identically on the
worker pods and in the unit tests, and so the image stays light.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class Partial:
    """A worker's accumulated sample counts."""

    inside: int
    total: int

    def pi(self) -> float:
        """The π estimate implied by this partial alone (0.0 before any sample)."""
        return 4.0 * self.inside / self.total if self.total else 0.0


def sample_batch(n: int, rng: random.Random | None = None) -> Partial:
    """Throw ``n`` random darts; count how many land inside the quarter circle.

    A dedicated ``random.Random`` instance is used so each worker (seeded
    distinctly) samples an independent stream — summing independent partials is
    what makes the distributed estimate valid.
    """
    r = rng or random
    inside = 0
    for _ in range(n):
        x = r.random()
        y = r.random()
        if x * x + y * y <= 1.0:
            inside += 1
    return Partial(inside=inside, total=n)


def estimate_pi(inside: int, total: int) -> float:
    """Combine aggregated counts into a single π estimate (0.0 if no samples)."""
    return 4.0 * inside / total if total else 0.0
