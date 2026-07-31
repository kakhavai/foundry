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
    assert match["value"] == "/collectors/weather/catalog"


def test_route_attaches_to_the_foundry_gateway():
    docs = render(VALUES_DIR / "weather" / "values.yaml")
    parent = routes(docs)[0]["spec"]["parentRefs"][0]

    assert parent["name"] == "foundry"
    assert parent["namespace"] == "envoy-gateway-system"


def test_route_strips_the_collector_prefix():
    """Without the rewrite the collector receives /collectors/weather/... and
    404s every request. Each rule now rewrites to its own service-relative
    path, not a shared bare "/" — see test_each_rule_rewrites_to_the_service_
    relative_path for the per-rule mapping."""
    docs = render(VALUES_DIR / "weather" / "values.yaml")
    for rule in routes(docs)[0]["spec"]["rules"]:
        filters = rule["filters"]
        rewrite = [f for f in filters if f["type"] == "URLRewrite"]
        assert len(rewrite) == 1
        path = rewrite[0]["urlRewrite"]["path"]
        assert path["type"] == "ReplacePrefixMatch"
        assert path["replacePrefixMatch"] in {"/catalog", "/signals", "/refresh"}


def test_route_backend_points_at_the_service_port():
    docs = render(VALUES_DIR / "weather" / "values.yaml")
    backend = routes(docs)[0]["spec"]["rules"][0]["backendRefs"][0]
    services = [d for d in docs if d.get("kind") == "Service"]

    assert backend["name"] == services[0]["metadata"]["name"]
    assert backend["port"] == services[0]["spec"]["ports"][0]["port"]


def _weather_route() -> dict:
    """The single rendered HTTPRoute for weather's values file, with one rule
    per gateway.publicPaths entry."""
    docs = render(VALUES_DIR / "weather" / "values.yaml")
    found = routes(docs)
    assert len(found) == 1
    return found[0]


def test_one_rule_per_public_path():
    rules = _weather_route()["spec"]["rules"]
    matched = [r["matches"][0]["path"]["value"] for r in rules]
    assert matched == [
        "/collectors/weather/catalog",
        "/collectors/weather/signals",
        "/collectors/weather/refresh",
    ]


def test_each_rule_rewrites_to_the_service_relative_path():
    """The gateway strips the collector prefix and nothing else, so
    /collectors/weather/signals/convergence still reaches /signals/convergence."""
    for rule in _weather_route()["spec"]["rules"]:
        matched = rule["matches"][0]["path"]["value"]
        rewrite = rule["filters"][0]["urlRewrite"]["path"]["replacePrefixMatch"]
        assert matched == f"/collectors/weather{rewrite}"


def test_health_is_not_published_at_the_edge():
    """Auth-exempt by necessity in-cluster; that is not a reason to publish it."""
    matched = [
        r["matches"][0]["path"]["value"] for r in _weather_route()["spec"]["rules"]
    ]
    assert not any(m.endswith("/health") for m in matched)


def test_metrics_is_not_published_at_the_edge():
    """In-cluster this exposes collector_* series — poll cadence, failure
    reasons, coverage, and auth-failure counts. Not an edge surface."""
    matched = [
        r["matches"][0]["path"]["value"] for r in _weather_route()["spec"]["rules"]
    ]
    assert not any(m.endswith("/metrics") for m in matched)


def test_no_rule_matches_the_bare_collector_prefix():
    """A bare prefix rule would republish everything and silently undo this."""
    matched = [
        r["matches"][0]["path"]["value"] for r in _weather_route()["spec"]["rules"]
    ]
    assert "/collectors/weather" not in matched
    assert "/collectors/weather/" not in matched


def test_empty_public_paths_fails_the_render(tmp_path):
    """Fail loudly rather than rendering a route that publishes nothing, or
    worse, falls back to a bare prefix."""
    values_file = tmp_path / "values.yaml"
    values_file.write_text(
        yaml.dump(
            {
                "service": {"name": "test", "port": 8000},
                "image": {"repository": "test"},
                "gateway": {
                    "enabled": True,
                    "pathPrefix": "/collectors/x",
                    "publicPaths": [],
                },
            }
        )
    )
    with pytest.raises(subprocess.CalledProcessError):
        render(values_file)


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
    overlay = (
        ROOT
        / "infra"
        / "gitops"
        / "envs"
        / "local"
        / values_file.parent.name
        / "values.yaml"
    )
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
    assert deployments, (
        f"{values_file.parent.name} rendered an HTTPRoute but no Deployment"
    )
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
