_PROXIMITY_WINDOW_MIN = 30.0  # deploys within this window of onset are "recent"
_PATH_OVERLAP_BONUS = 0.3
_CONTRADICTION_PENALTY = 0.4


def score_deploy(
    minutes_before_anomaly: float,
    touched_paths: list[str],
    service: str,
    external_dependency_implicated: bool,
) -> float:
    """Correlate a deploy with the incident onset. Score rises when the deploy is
    recent and touched the affected service; it falls when an external/downstream
    dependency is independently implicated (the contradiction penalty), since that
    points away from this deploy. Clamped to [0, 1]."""

    # Proximity: linear decay from 1.0 at onset to 0.0 at the window edge.
    if minutes_before_anomaly < 0:
        proximity = 0.0
    elif minutes_before_anomaly <= _PROXIMITY_WINDOW_MIN:
        proximity = 1.0 - (minutes_before_anomaly / _PROXIMITY_WINDOW_MIN)
    else:
        proximity = 0.0

    score = 0.6 * proximity

    # Path overlap bonus only applies when there's meaningful proximity (> 0).
    if proximity > 0 and any(p.startswith(f"services/{service}/") for p in touched_paths):
        score += _PATH_OVERLAP_BONUS

    if external_dependency_implicated:
        score -= _CONTRADICTION_PENALTY

    return max(0.0, min(1.0, score))
