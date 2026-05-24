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

## Step 7: Report

For each layer, record pass/fail with actual output — not "looks fine" but the real HTTP status or first line of metrics. Any failure stops the PR.

## Layers That Must All Pass

| Layer | What to check |
|---|---|
| Unit tests | `pytest -v` — 0 failures |
| Service startup | Process starts without errors |
| HTTP endpoints | Correct status codes and response shapes |
| Docker build | Image builds without error |
| Container startup | App starts inside container (check logs) |
| Helm render | `helm template` produces valid manifests |
| Helm lint | `helm lint` — 0 failures |
| CI action refs | All `uses: ./.github/actions/...` resolve to real files |
