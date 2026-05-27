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

## Step 7: Run the Scripts and Verify They Work

`scripts/deploy-local.py` and `scripts/stack-up.py` both maintain a hardcoded `SERVICES` registry. If the PR adds, removes, renames, or changes a service port, those scripts must be updated. Run all of this — don't just read it.

### 7a: Static scope check (catches registry drift before wasting a build)

```python
import ast, sys, importlib.util
import yaml
from pathlib import Path

ROOT = Path(".")
SCRIPTS = ["scripts/deploy-local.py", "scripts/stack-up.py"]

for script in SCRIPTS:
    try:
        ast.parse(Path(script).read_text())
        print(f"OK  syntax: {script}")
    except SyntaxError as e:
        print(f"ERR syntax: {script}: {e}"); sys.exit(1)

for script in SCRIPTS:
    spec = importlib.util.spec_from_file_location("_s", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in mod.SERVICES:
        svc_dir = ROOT / "services" / name
        values_file = ROOT / "helm/values" / name / "values.yaml"
        print("OK " if svc_dir.exists() else "ERR", f"service dir:   services/{name}/")
        print("OK " if values_file.exists() else "ERR", f"helm values:   helm/values/{name}/values.yaml")

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
    print("OK " if helm_port == script_port else "ERR", f"port match {name}: script={script_port} helm={helm_port}")
```

### 7b: Run deploy-local.py for every service

Requires: docker running, `kind` cluster named `foundry` running.

```bash
python scripts/deploy-local.py weather
python scripts/deploy-local.py player-projections
```

Both must exit 0. A non-zero exit means docker build, kind load, or helm upgrade failed — stop and fix before the PR.

### 7c: Run stack-up.py and verify the full stack comes up

```bash
python scripts/stack-up.py
```

Let it run until it prints the "Stack is up" access URL table, then Ctrl+C. Verify:
- No step exits non-zero (cluster, helmfile, deploy, kubectl wait)
- The access URL table lists every service with the correct port
- Port-forwards bind without errors in the output before Ctrl+C

A clean run looks like:
```
$ kind create cluster ... (or "already running")
$ helmfile apply
$ python scripts/deploy-local.py weather   → exit 0
$ python scripts/deploy-local.py player-projections  → exit 0
$ kubectl wait --for=condition=ready pod ...
==================================================
Stack is up. Access your services at:
  weather               http://localhost:8000
  player-projections    http://localhost:8001
  Grafana               http://localhost:3000  (admin / admin)
  ...
==================================================
```

Any service missing from the table or any step that exits non-zero is a failure — fix before the PR ships.

## Step 8: Report

For each layer, record pass/fail with actual output — not "looks fine" but the real HTTP status or first line of metrics. Any failure stops the PR.

## Layers That Must All Pass

| Layer | What to check |
|---|---|
| Unit tests | `pytest -v` — 0 failures |
| Lint | `uv run ruff check .` and `uv run ruff format --check .` — 0 errors |
| Service startup | Process starts without errors |
| HTTP endpoints | Correct status codes and response shapes |
| Docker build | Image builds without error |
| Container startup | App starts inside container (check logs) |
| Helm render | `helm template` produces valid manifests |
| Helm lint | `helm lint` — 0 failures |
| CI action refs | All `uses: ./.github/actions/...` resolve to real files |
| Scripts in scope | `deploy-local.py` and `stack-up.py` SERVICES match disk, ports match Helm |
