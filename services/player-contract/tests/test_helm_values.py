"""The deployment facts this service's own code depends on.

The repo-root `tests/test_collector_registry.py` already pins the registry
against `gateway.pathPrefix`, and `tests/test_collector_tooling.py` pins that no
collector name is hard-coded into the scripts. Neither can see the two things
that fail SILENTLY for this collector specifically:

* `PLAYER_IDENTITY_URL` missing — the pod starts, reports Healthy, and publishes
  nothing forever, because an empty value is a deliberate refusal rather than a
  stub; and
* `CAPTURE_ENABLED` flipped to `true` — a loop that polls a document frozen
  since 2022-05-29 and writes four-year-old contracts into an append-only lake
  as though they were current.

Read as YAML rather than rendered through Helm because that is what the drift is
about: the values file is the artifact Kubernetes applies.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
VALUES = yaml.safe_load(
    (ROOT / "helm" / "values" / "player-contract" / "values.yaml").read_text()
)


def _env(name: str) -> str | None:
    for entry in VALUES["extraEnv"]:
        if entry["name"] == name:
            return entry.get("value")
    return None


def test_the_port_matches_the_readme_and_the_container():
    assert VALUES["service"]["port"] == 8024
    assert VALUES["containerPort"] == 8024


def test_only_the_standard_contract_surface_is_published():
    """This collector has no route beyond the standard five — the incentive
    query route (`GET /signals/incentives`) belonged to the deferred half. A
    path appearing here that no handler serves publishes a 404 at the edge."""
    assert VALUES["gateway"]["publicPaths"] == ["/catalog", "/signals", "/refresh"]


def test_health_and_metrics_are_NOT_published_at_the_gateway():
    """They are exempt from bearer auth in-process — a kubelet probe and a
    Prometheus scrape cannot carry a token — which is a reason to reach them
    in-cluster, not at the edge."""
    published = VALUES["gateway"]["publicPaths"]
    assert "/health" not in published
    assert "/metrics" not in published


def test_the_identity_seam_is_pointed_at_the_real_crosswalk():
    """An empty value is a refusal, not a stub. This feed keys players by
    display NAME and carries no id this collector could fall back to, so without
    `player-identity` not one row can be resolved — and a values file that
    forgot this deploys a pod that reports Healthy and publishes nothing."""
    assert _env("PLAYER_IDENTITY_URL") == "http://player-identity:8002"


def test_capture_is_disabled():
    """Not merely the scaffolder's default — a decision. The upstream `.csv.gz`
    has not been regenerated since 2022-05-29 while the release around it is
    refreshed daily, so a loop buys no freshness and writes 2022 contracts into
    an append-only lake. `broadcast-context`'s vacuous-guard exception does not
    apply: nothing here reads a prior lake snapshot back, so one dispatched
    `/refresh` produces exactly what a season-long loop would."""
    assert _env("CAPTURE_ENABLED") == "false"


def test_there_is_no_smoke_hook_to_dispatch_a_refresh():
    """A dispatched `POST /refresh` reaches the third party REGARDLESS of
    `CAPTURE_ENABLED`. This collector has no route beyond the standard five, so
    it has no reason for a hook — and the standard surface is asserted for every
    registered collector automatically."""
    assert not (ROOT / "services" / "player-contract" / "smoke.sh").exists()


def test_the_gateway_path_matches_the_registry():
    registry = yaml.safe_load(
        (ROOT / "contracts" / "collector-registry.yaml").read_text()
    )
    entry = next(c for c in registry["collectors"] if c["name"] == "player-contract")
    assert entry["path"] == VALUES["gateway"]["pathPrefix"]
    assert entry["envelope_version"] == "1"
    assert entry["scope_aware"] is True
    assert entry["signal_types"] == ["player_contract_status"]


def test_the_registry_does_not_claim_an_incentive_surface():
    """`player_incentive_progress` was split out to a deferred
    `player-incentives` collector during 8E. A registry entry that still listed
    it would tell the projections generator to call a route that does not
    exist."""
    registry = (ROOT / "contracts" / "collector-registry.yaml").read_text()
    entry = registry[registry.index("- name: player-contract") :]
    entry = entry[: entry.find("\n  - name: ") if "\n  - name: " in entry else None]
    assert (
        "player_incentive_progress"
        not in entry.split("signal_types:")[1].split("\n")[0]
    )
