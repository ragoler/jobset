"""Unit tests for the REAL Monte Carlo compute (no cluster needed).

This is the actual sampling code the worker pods run — verifying it converges
toward π for large N is the unit-tested proof that the LIVE workload computes
something real (the project forbids mocking the computation).
"""

import math
import random

import montecarlo


def test_sample_batch_counts_inside_le_total():
    p = montecarlo.sample_batch(10_000, random.Random(1))
    assert 0 <= p.inside <= p.total == 10_000


def test_estimate_pi_zero_when_no_samples():
    assert montecarlo.estimate_pi(0, 0) == 0.0
    assert montecarlo.Partial(0, 0).pi() == 0.0


def test_quarter_circle_ratio_is_quarter_pi():
    """inside/total -> π/4, so 4*inside/total -> π for large N."""
    p = montecarlo.sample_batch(400_000, random.Random(42))
    est = p.pi()
    # 400k darts: well within 0.05 of π in practice.
    assert math.isclose(est, math.pi, abs_tol=0.05), f"got {est}"


def test_aggregation_of_independent_partials_converges():
    """Summing independent worker partials (as the leader does) converges to π."""
    inside = total = 0
    for seed in range(8):  # 8 'workers', distinct seeds -> independent streams
        p = montecarlo.sample_batch(100_000, random.Random(1000 + seed))
        inside += p.inside
        total += p.total
    est = montecarlo.estimate_pi(inside, total)
    assert total == 800_000
    assert math.isclose(est, math.pi, abs_tol=0.02), f"got {est}"


def test_distinct_seeds_give_distinct_streams():
    # Two distinctly-seeded RNGs draw different sample sequences (the aggregate
    # inside-counts could collide by chance, so compare the raw draws instead).
    seq_a = [random.Random(1).random() for _ in range(5)]
    seq_b = [random.Random(2).random() for _ in range(5)]
    assert seq_a != seq_b
    # And the same seed is reproducible (so worker restarts are deterministic).
    assert [random.Random(1).random() for _ in range(5)] == seq_a


def test_more_samples_reduce_error_on_average():
    """Mean absolute error shrinks as N grows (law of large numbers)."""
    def mean_err(n, trials=5):
        errs = []
        for t in range(trials):
            p = montecarlo.sample_batch(n, random.Random(7 * t + 1))
            errs.append(abs(p.pi() - math.pi))
        return sum(errs) / len(errs)

    assert mean_err(200_000) < mean_err(2_000)
