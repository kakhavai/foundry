# PR UAT

Verify a PR end-to-end before it ships. Tests pass and YAML validates locally but containers can crash at startup, ports can be wrong, module imports can break, and Helm values can reference non-existent keys. This walks through every layer.

**Required before opening any final PR to main.**

## Step 1: Understand What the PR Changes

Read the PR diff. For each service, action, or infrastructure component changed, build a checklist:
- Which services were added or modified?
- Which endpoints are new or changed?
- Which Docker images need to build?
- Which Helm charts or values were touched?
- Which CI actions or workflow files changed?

## Step 2: Run the Test Suite

```bash
cd services/<name>
uv run pytest -v
```

All tests must pass before continuing.

## Step 3: Start Each Service and Hit Every Endpoint

```bash
uv run uvicorn <service>.main:app --port <port>

curl http://localhost:<port>/health
curl http://localhost:<port>/metrics | head -5
# exercise all feature endpoints — happy path AND error cases
```

Check: correct status codes, correct response shapes, `/metrics` returns `# HELP ...`, `/health` returns `{"status": "ok"}`.

## Step 4: Docker Build and Container Run

```bash
docker build -t <service>:smoke services/<service>/
docker run -d --name <service>-smoke -p <host-port>:<container-port> <service>:smoke
sleep 3
curl http://localhost:<host-port>/health
docker rm -f <service>-smoke
```

A successful `docker build` does not mean the app starts. Always run it.

## Step 5: Helm Render and Lint

```bash
helm template <service> helm/charts/generic-service -f helm/values/<service>/values.yaml
helm lint helm/charts/generic-service -f helm/values/<service>/values.yaml
```

Verify the rendered manifests show the correct port, image name, and env vars.

## Step 6: Verify CI Action References Resolve

```python
import yaml, os
for wf in ['.github/workflows/<service>.yml']:
    data = yaml.safe_load(open(wf))
    for job in data['jobs'].values():
        for step in job.get('steps', []):
            uses = step.get('uses', '')
            if uses.startswith('./.github/actions/'):
                path = uses[2:] + '/action.yml'
                print('OK' if os.path.exists(path) else f'MISSING: {path}')
```

## Step 7: Verify Scripts Stay in Scope

`scripts/deploy-local.py` and `scripts/stack-up.py` both maintain a hardcoded `SERVICES` registry. If the PR adds, removes, renames, or reports a service, those scripts must be updated. Run this check:

```python
import ast, sys, importlib.util
import yaml
from pathlib import Path

ROOT = Path(".")
SCRIPTS = ["scripts/deploy-local.py", "scripts/stack-up.py"]

# 1. Syntax check
for script in SCRIPTS:
    try:
        ast.parse(Path(script).read_text())
        print(f"OK  syntax: {script}")
    except SyntaxError as e:
        print(f"ERR syntax: {script}: {e}")
        sys.exit(1)

# 2. Load SERVICES from each script and cross-reference disk
for script in SCRIPTS:
    spec = importlib.util.spec_from_file_location("_s", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in mod.SERVICES:
        svc_dir = ROOT / "services" / name
        values_file = ROOT / "helm/values" / name / "values.yaml"
        print("OK " if svc_dir.exists() else "ERR", f"service dir:   services/{name}/")
        print("OK " if values_file.exists() else "ERR", f"helm values:   helm/values/{name}/values.yaml")

# 3. Port consistency — script port must match helm values service.port
spec = importlib.util.spec_from_file_location("_d", "scripts/deploy-local.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for name, cfg in mod.SERVICES.items():
    values_file = ROOT / "helm/values" / name / "values.yaml"
    if not values_file.exists():
        continue
    values = yaml.safe_load(values_file.read_text())
    helm_port = values.get("service", {}).get("port")
    script_port = cfg.get("port")
    match = helm_port == script_port
    print("OK " if match else "ERR", f"port match {name}: script={script_port} helm={helm_port}")
```

What to verify manually:
- Any service added by this PR appears in **both** `SERVICES` dicts.
- Any service removed or renamed is cleaned out of both scripts.
- Ports in `deploy-local.py` and `stack-up.py` match `helm/values/<service>/values.yaml → service.port`.
- `stack-up.py` pod label `app.kubernetes.io/name=<service>` matches what the Helm chart renders.

## Step 8: Report

For each layer, record pass/fail with actual output — not "looks fine" but the real HTTP status or first line of metrics. Any failure stops the PR.

## Layers That Must All Pass

| Layer | What to check |
|---|---|
| Unit tests | `pytest -v` — 0 failures |
| Lint | `ruff check` and `ruff format --check` — 0 errors |
| Service startup | Process starts without errors |
| HTTP endpoints | Correct status codes and response shapes |
| Docker build | Image builds without error |
| Container startup | App starts inside container (check logs) |
| Helm render | `helm template` produces valid manifests |
| Helm lint | `helm lint` — 0 failures |
| CI action refs | All `uses: ./.github/actions/...` resolve to real files |
| Scripts in scope | `deploy-local.py` and `stack-up.py` SERVICES match disk, ports match Helm |
