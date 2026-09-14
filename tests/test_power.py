

def test_it_agrees_with_the_one_mcnemar_the_verdict_uses():
    """`two_sided_p` used to carry its own copy of the exact sum. Two copies
    of the arithmetic every verdict rests on is the drift this project keeps
    paying for, so below 60 disagreements it delegates -- and this asserts
    the delegation, plus that the log-space path above 60 still matches to
    the digits anyone reports."""
    from context_lift.metrics import mcnemar_exact
    from context_lift.power import two_sided_p

    for fixed, broke in [(0, 0), (4, 0), (5, 0), (6, 0), (15, 3), (4, 2),
                         (1, 3), (30, 30), (60, 0)]:
        assert two_sided_p(fixed, broke) == mcnemar_exact(fixed, broke).p_value, \
            (fixed, broke)

    # above the threshold the fast path takes over; it must still agree to
    # more digits than this project ever prints
    for fixed, broke in [(282, 19), (100, 40), (200, 200)]:
        fast = two_sided_p(fixed, broke)
        exact = mcnemar_exact(fixed, broke).p_value
        assert abs(fast - exact) <= 1e-9 * max(exact, 1e-300), (fixed, broke, fast, exact)
