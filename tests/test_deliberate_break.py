def test_ci_actually_runs_the_suite() -> None:
    """Deliberate-break demo: CI must be able to say no. Reverted after."""
    assert False, "intentional red to prove CI is not decorative"
