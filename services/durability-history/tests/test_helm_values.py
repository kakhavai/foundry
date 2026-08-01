"""The deployment facts this service's own code depends on.

The repo-root `tests/test_collector_registry.py` already pins the registry
against `gateway.pathPrefix`, and `tests/test_collector_tooling.py` pins that no
collector name is hard-coded into the scripts. Neither can see the two things
that fail SILENTLY for this collector specifically:

* an extra route missing from `gateway.publicPaths` — it works in-cluster and
  404s at the edge, which reads like a broken handler rather than a missing line
  in a values file; and
* `CAPTURE_ENABLED` flipped to `true` while `smoke.sh` posts a `/refresh` — a
  dispatched refresh reaches the upstream regardless of the flag, so that
  combination puts 43.8 MB of third-party traffic on every PR.

Read as YAML rather than rendered through Helm because that is what the drift is
about: the values file is the artifact Kubernetes applies.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
VALUES = yaml.safe_load(
    (ROOT / "helm" / "values" / "durability-history" / "values.yaml").read_text()
)
SMOKE = (ROOT / "services" / "durability-history" / "smoke.sh").read_text()


def _env(name: str) -> str | None:
    for entry in VALUES["extraEnv"]:
        if entry["name"] == name:
            return entry.get("value")
    return None


def test_the_port_matches_the_readme_and_the_container():
    assert VALUES["service"]["port"] == 8020
    assert VALUES["containerPort"] == 8020


def test_the_extra_route_is_reachable_through_the_gateway():
    """`/signals` is a PathPrefix match, so it publishes `/signals/return-profile`
    too. Omit it and BOTH 404 at the edge while working in-cluster."""
    assert "/signals" in VALUES["gateway"]["publicPaths"]


def test_health_and_metrics_are_NOT_published_at_the_gateway():
    """They are exempt from bearer auth in-process — a kubelet probe and a
    Prometheus scrape cannot carry a token — which is a reason to reach them
    in-cluster, not at the edge."""
    published = VALUES["gateway"]["publicPaths"]
    assert "/health" not in published
    assert "/metrics" not in published


def test_the_smoke_hook_does_not_dispatch_a_refresh_while_capture_is_disabled():
    """A dispatched `POST /refresh` reaches the upstream REGARDLESS of
    `CAPTURE_ENABLED`, so a hook that posts one puts 43.8 MB of third-party
    nflverse traffic on every PR.

    Comment lines are stripped first: the hook explains at length why it does
    NOT post one, and a bare substring check fails on the explanation. The
    stripping is the difference between a guard and a tripwire.
    """
    executable = "\n".join(
        line for line in SMOKE.splitlines() if not line.lstrip().startswith("#")
    )
    if _env("CAPTURE_ENABLED") == "false":
        assert "/refresh" not in executable, (
            "smoke.sh posts a refresh while CAPTURE_ENABLED is false — that "
            "reaches the third party on every PR"
        )


def test_capture_is_disabled_by_default():
    """43.8 MB on a cold process, and the Kind cluster is rebuilt every CI run.
    Flipping this is a deliberate act on a long-lived cluster, not a default."""
    assert _env("CAPTURE_ENABLED") == "false"


def test_the_identity_seam_is_pointed_at_the_real_crosswalk():
    """An empty value is a refusal, not a stub: this collector fails closed with
    `identity_unavailable` rather than minting ids of its own, so a values file
    that forgot this deploys a pod that reports Healthy and publishes nothing."""
    assert _env("PLAYER_IDENTITY_URL") == "http://player-identity:8002"


def test_the_cost_and_rule_dials_are_declared_rather_than_implicit():
    """All three change what published rows MEAN. Leaving them to the code's
    defaults would make a change invisible in the artifact Kubernetes applies."""
    assert _env("DURABILITY_HISTORY_SEASONS") == "3"
    assert _env("RECURRENCE_WINDOW_DAYS") == "90"
    assert _env("MIN_SAMPLE_EVENTS") == "3"


def test_the_declared_dials_agree_with_the_code_defaults():
    """Two sources for one number drift. Pinned here so a change to either side
    is a visible edit rather than a silent disagreement between the values file
    and the module constant."""
    from durability_history import derive
    from durability_history.adapters import upstream

    assert int(_env("DURABILITY_HISTORY_SEASONS")) == upstream.DEFAULT_HISTORY_SEASONS
    assert int(_env("RECURRENCE_WINDOW_DAYS")) == upstream.RECURRENCE_WINDOW_DAYS
    assert int(_env("MIN_SAMPLE_EVENTS")) == derive.MIN_SAMPLE_EVENTS


def test_the_registry_does_not_claim_a_taxonomy_agreement_that_does_not_exist():
    """`injury-report` and this collector read DIFFERENT feeds and publish
    DIFFERENT `absence_reason` enums. An earlier revision of the registry entry
    claimed both were the same; a generator joining a current-week reason to a
    historical one on that claim gets `discipline` where it expects `suspension`
    and two values where it expects one.

    The registry is where the generator reads the fleet's inventory, so the
    translation table belongs there — and this test is what stops it silently
    reverting to a claim of agreement.
    """
    registry = (ROOT / "contracts" / "collector-registry.yaml").read_text()
    entry = registry[registry.index("- name: durability-history") :]

    assert "MUST TRANSLATE" in entry, (
        "the registry no longer warns that the two absence_reason enums differ"
    )
    for pairing in ("discipline", "suspension", "non_injury_other", "undesignated"):
        assert pairing in entry, pairing
    assert "the same feed" not in entry, (
        "the registry is claiming a shared feed again — injury-report reads "
        "INJURY_REPORT_URL, this collector reads nflverse injuries_{season}.csv"
    )


def test_the_published_enum_still_matches_the_mapping_the_registry_documents():
    """The table in the registry is prose; this is what keeps it true. A value
    added to `ABSENCE_REASONS` without a row in that table leaves a generator
    with an untranslatable reason and no warning."""
    from durability_history.events import ABSENCE_REASONS

    registry = (ROOT / "contracts" / "collector-registry.yaml").read_text()
    entry = registry[registry.index("- name: durability-history") :]
    for reason in ABSENCE_REASONS:
        assert reason in entry, (
            f"{reason!r} is published but is not in the registry's translation "
            "table for injury-report"
        )


def test_the_gateway_path_matches_the_registry():
    registry = yaml.safe_load(
        (ROOT / "contracts" / "collector-registry.yaml").read_text()
    )
    entry = next(c for c in registry["collectors"] if c["name"] == "durability-history")
    assert entry["path"] == VALUES["gateway"]["pathPrefix"]
    assert entry["envelope_version"] == "1"
    assert entry["scope_aware"] is True
