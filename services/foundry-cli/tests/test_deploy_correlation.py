from foundry.triage.detectors.deploy_correlation import score_deploy


def test_recent_deploy_touching_service_scores_high():
    score = score_deploy(
        minutes_before_anomaly=3.0,
        touched_paths=["services/weather/weather/client.py"],
        service="weather",
        external_dependency_implicated=False,
    )
    assert score > 0.6


def test_old_deploy_scores_low():
    score = score_deploy(
        minutes_before_anomaly=600.0,
        touched_paths=["services/weather/weather/client.py"],
        service="weather",
        external_dependency_implicated=False,
    )
    assert score < 0.3


def test_external_dependency_applies_contradiction_penalty():
    near = dict(
        minutes_before_anomaly=3.0,
        touched_paths=["services/weather/x.py"],
        service="weather",
    )
    with_contradiction = score_deploy(**near, external_dependency_implicated=True)
    without = score_deploy(**near, external_dependency_implicated=False)
    assert with_contradiction < without


def test_deploy_not_touching_service_scores_lower_than_one_that_does():
    base = dict(
        minutes_before_anomaly=3.0,
        service="weather",
        external_dependency_implicated=False,
    )
    touching = score_deploy(touched_paths=["services/weather/main.py"], **base)
    unrelated = score_deploy(
        touched_paths=["services/player-projections/main.py"], **base
    )
    assert touching > unrelated


def test_score_clamped_to_unit_interval():
    score = score_deploy(
        minutes_before_anomaly=0.0,
        touched_paths=["services/weather/x.py"],
        service="weather",
        external_dependency_implicated=False,
    )
    assert 0.0 <= score <= 1.0
