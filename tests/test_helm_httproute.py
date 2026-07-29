"""Gateway render assertions, and the guard against exposing a collector
without protecting it.

`tests/` is the platform suite — things no per-service test can see. A service
test cannot know whether its own values file routes traffic in from outside the
cluster, which is exactly the mistake worth catching before it deploys.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm" / "charts" / "generic-service"
VALUES_DIR = ROOT / "helm" / "values"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm binary not installed"
)


def render(*values_files: Path, **sets: str) -> list[dict]:
    cmd = ["helm", "template", "test", str(CHART)]
    for path in values_files:
        cmd += ["-f", str(path)]
    for key, value in sets.items():
        cmd += ["--set", f"{key.replace('__', '.')}={value}"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def routes(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("kind") == "HTTPRoute"]


def test_no_httproute_by_default():
    """player-projections is not a collector and must not be exposed."""
    docs = render(service__name="test", image__repository="test")

    assert routes(docs) == []


def test_player_projections_values_render_no_httproute():
    docs = render(VALUES_DIR / "player-projections" / "values.yaml")

    assert routes(docs) == []


def test_weather_values_render_one_httproute_at_the_collector_path():
    docs = render(VALUES_DIR / "weather" / "values.yaml")
    found = routes(docs)

    assert len(found) == 1
    rule = found[0]["spec"]["rules"][0]
    match = rule["matches"][0]["path"]
    assert match["type"] == "PathPrefix"
    assert match["value"] == "/collectors/weather"


def test_route_attaches_to_the_foundry_gateway():
    docs = render(VALUES_DIR / "weather" / "values.yaml")
    parent = routes(docs)[0]["spec"]["parentRefs"][0]

    assert parent["name"] == "foundry"
    assert parent["namespace"] == "envoy-gateway-system"


def test_route_strips_the_collector_prefix():
    """Without the rewrite the collector receives /collectors/weather/... and
    404s every request."""
    docs = render(VALUES_DIR / "weather" / "values.yaml")
    filters = routes(docs)[0]["spec"]["rules"][0]["filters"]

    rewrite = [f for f in filters if f["type"] == "URLRewrite"]
    assert len(rewrite) == 1
    path = rewrite[0]["urlRewrite"]["path"]
    assert path["type"] == "ReplacePrefixMatch"
    assert path["replacePrefixMatch"] == "/"


def test_route_backend_points_at_the_service_port():
    docs = render(VALUES_DIR / "weather" / "values.yaml")
    backend = routes(docs)[0]["spec"]["rules"][0]["backendRefs"][0]
    services = [d for d in docs if d.get("kind") == "Service"]

    assert backend["name"] == services[0]["metadata"]["name"]
    assert backend["port"] == services[0]["spec"]["ports"][0]["port"]


def test_enabling_the_gateway_without_a_path_prefix_fails_the_render():
    with pytest.raises(subprocess.CalledProcessError) as exc:
        render(
            service__name="test",
            image__repository="test",
            gateway__enabled="true",
        )

    assert "gateway.pathPrefix" in exc.value.stderr


def _value_layers(values_file: Path) -> list[Path]:
    """The value-file stack ArgoCD actually applies for this service.

    `infra/gitops/argo/<name>.yaml` layers the base `helm/values/<name>/values.yaml`
    with an env overlay at `infra/gitops/envs/local/<name>/values.yaml`. A service
    could enable the gateway in either layer, so both must be rendered together —
    reading just the base file (as this test used to) misses a `gateway.enabled`
    set only in the overlay.
    """
    layers = [values_file]
    overlay = ROOT / "infra" / "gitops" / "envs" / "local" / values_file.parent.name / "values.yaml"
    if overlay.exists():
        layers.append(overlay)
    return layers


@pytest.mark.parametrize(
    "values_file", sorted(VALUES_DIR.glob("*/values.yaml")), ids=lambda p: p.parent.name
)
def test_gateway_enabled_services_require_a_collector_token(values_file):
    """The failure this PR is most exposed to: a service routed in from outside
    the cluster whose values file never wires up the token it authenticates
    with. Auth is enforced in-process, so the pod would answer 503 to everyone
    — but an author who then "fixed" it by relaxing the service would have
    published an open collector. Catch the missing Secret at render time,
    using the exact value-file stack ArgoCD applies (base + env overlay), not
    a parse of the base file alone — a service can flip `gateway.enabled` in
    either layer.
    """
    docs = render(*_value_layers(values_file))
    found = routes(docs)
    if not found:
        pytest.skip("gateway not enabled for this service")

    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    assert deployments, f"{values_file.parent.name} rendered an HTTPRoute but no Deployment"
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    env_vars = [env for c in containers for env in (c.get("env") or [])]

    tokens = [env for env in env_vars if env.get("name") == "COLLECTOR_TOKEN"]
    assert len(tokens) == 1, (
        f"{values_file.parent.name} enables the collector gateway but does not "
        "declare a COLLECTOR_TOKEN env var on the rendered Deployment"
    )
    assert "secretKeyRef" in (tokens[0].get("valueFrom") or {}), (
        f"{values_file.parent.name}'s COLLECTOR_TOKEN must come from a "
        "secretKeyRef, never a literal value"
    )


def test_gateway_path_prefixes_are_unique_across_collectors():
    """26 collectors are expected to copy this values file. A forgotten
    `pathPrefix` edit produces two HTTPRoutes claiming the same path; Gateway
    API resolves the conflict by creation timestamp, so the loser is silently
    unreachable through the gateway rather than failing loudly.
    """
    prefixes: dict[str, str] = {}
    for values_file in sorted(VALUES_DIR.glob("*/values.yaml")):
        docs = render(*_value_layers(values_file))
        for route in routes(docs):
            for rule in route["spec"]["rules"]:
                for match in rule["matches"]:
                    prefix = match["path"]["value"]
                    owner = values_file.parent.name
                    assert prefix not in prefixes, (
                        f"{owner!r} and {prefixes.get(prefix)!r} both claim "
                        f"gateway.pathPrefix {prefix!r}"
                    )
                    prefixes[prefix] = owner
