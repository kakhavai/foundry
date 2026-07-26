from foundry.triage.models.suspect import SuspectScore


def test_total_sums_supports_minus_penalty():
    score = SuspectScore(
        suspect="api.github.com upstream dependency",
        metric_support=0.5,
        trace_support=0.4,
        deploy_support=0.0,
        contradiction_penalty=0.1,
    )
    assert abs(score.total - 0.8) < 1e-9


def test_total_defaults_are_zero():
    score = SuspectScore(suspect="weather v0.4.1 deploy")
    assert score.total == 0.0


def test_log_and_trace_support_default_zero_for_m1():
    # Milestone 1 has no log/trace detectors; those slots must default to 0.
    score = SuspectScore(suspect="x", metric_support=0.3, deploy_support=0.2)
    assert abs(score.total - 0.5) < 1e-9
