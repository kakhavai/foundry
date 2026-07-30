"""Guards the liveness-probe timeout the spike load shape exposed.

`tests/load/spike.js` restarted player-projections reproducibly at 500 RPS
against a 250m CPU limit: with Kubernetes' default timeoutSeconds: 1, a
saturated event loop fails /health inside a second and the kubelet kills a
container that is merely overloaded, not hung. The fix in
helm/charts/generic-service/templates/deployment.yaml raises the liveness
probe's timeoutSeconds above the default so a future edit that silently
restores the default (or removes the override) is caught here instead of on
the next traffic spike.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm" / "charts" / "generic-service"
VALUES_DIR = ROOT / "helm" / "values"

# Kubernetes fills this in when a probe omits timeoutSeconds. The whole
# defect was this default being too short for a busy-but-alive event loop.
KUBE_DEFAULT_PROBE_TIMEOUT = 1

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


def _container(docs: list[dict]) -> dict:
    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    assert deployments, "chart rendered no Deployment"
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def test_liveness_timeout_exceeds_the_kubernetes_default():
    """The regression this test exists to catch: a future edit that drops the
    override and silently falls back to Kubernetes' one-second default,
    reproducing the restart loop tests/load/spike.js found.
    """
    docs = render(VALUES_DIR / "player-projections" / "values.yaml")
    liveness = _container(docs)["livenessProbe"]

    assert liveness["timeoutSeconds"] > KUBE_DEFAULT_PROBE_TIMEOUT, (
        "livenessProbe.timeoutSeconds must exceed the Kubernetes default of "
        f"{KUBE_DEFAULT_PROBE_TIMEOUT}s, or a saturated-but-alive event loop "
        "trips the probe and the kubelet restarts a merely overloaded "
        "container — the exact failure tests/load/spike.js reproduced"
    )


def test_readiness_timeout_matches_liveness():
    """Readiness was deliberately loosened to the same timeout as liveness
    (see the comment in deployment.yaml): every service here runs
    replicaCount: 1, so failing readiness under load does not shed traffic to
    a healthy sibling, it drops the only Endpoint and turns overload into a
    hard connection error. This pins that decision so a future edit cannot
    silently re-tighten readiness without the change being visible here.
    """
    docs = render(VALUES_DIR / "player-projections" / "values.yaml")
    container = _container(docs)

    assert (
        container["readinessProbe"]["timeoutSeconds"]
        == container["livenessProbe"]["timeoutSeconds"]
    )
