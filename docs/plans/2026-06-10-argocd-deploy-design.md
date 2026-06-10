# ArgoCD Deploy Script — Design Spec

**Date:** 2026-06-10
**Status:** Approved

---

## Problem

`stack-up.py` buries ArgoCD installation as one step among many. `deploy-local.py` bypasses ArgoCD entirely (direct `helm upgrade --install`), which conflicts with ArgoCD's `selfHeal: true` and gets reverted within seconds. There is no standalone script that owns the ArgoCD lifecycle: install, health verification, app-of-apps bootstrap, sync monitoring, UI access, and environment promotion.

---

## Solution

A single standalone script `scripts/argocd-deploy.py` with five sub-commands. It does not depend on `stack-up.py` or `deploy-local.py`. It is the authoritative entrypoint for everything ArgoCD-related in the repo.

---

## Sub-commands

### `install`

```
python scripts/argocd-deploy.py install --env local [--context <ctx>]
```

1. Runs `helmfile repos` + `helmfile apply` in `infra/argo/`, selecting `values.yaml` (local) or `values-<env>.yaml` (staging/prod) if it exists.
2. Waits for `deployment/argocd-server` in namespace `argocd` to be available (`kubectl wait --for=condition=available --timeout=180s`).
3. Applies `infra/gitops/argo/app-of-apps.yaml`.
4. Polls all ArgoCD Applications until every one is `Synced + Healthy` (or times out).
5. Prints the decoded admin password and ArgoCD UI URL.
6. Exits — does not block. Run `ui` separately to access the UI.

### `verify`

```
python scripts/argocd-deploy.py verify --env local [--context <ctx>]
```

Read-only. Safe to run against any live cluster without side effects.

1. Checks that ArgoCD pods are running in namespace `argocd`.
2. Checks that every Application in namespace `argocd` is `Synced + Healthy`.
3. Triggers a manual `argocd app refresh` equivalent (`kubectl annotate application ... refresh=normal`) on each app and confirms it completes without error — verifies ArgoCD can reach the GitHub repo.
4. Prints a status table: Application name | Sync status | Health status | Last sync time.
5. Exit code 0 = everything healthy. Non-zero = something broken (details printed).

### `promote`

```
python scripts/argocd-deploy.py promote <service> --from local --to staging
```

1. Reads the current image tag from `infra/gitops/envs/<from>/<service>/values.yaml`.
2. Creates `infra/gitops/envs/<to>/<service>/` if it does not exist.
3. Writes the same tag to `infra/gitops/envs/<to>/<service>/values.yaml`.
4. Commits and pushes: `chore(gitops): promote <service> from <from> to <to> @ <tag>`.
5. Tails the `<to>` env Application (same polling loop as `install` step 4) until `Synced + Healthy` or timeout.

### `watch`

```
python scripts/argocd-deploy.py watch <service> --env local [--context <ctx>] [--timeout 180]
```

Observe an in-progress or upcoming deploy without triggering anything.

1. Polls `kubectl get application <service> -n argocd -o jsonpath=...` every 3 seconds.
2. Concurrently runs `kubectl rollout status deployment/<service>` and streams its output.
3. Exits when the Application is `Synced + Healthy`, or after `--timeout` seconds with a non-zero exit code.

### `ui`

```
python scripts/argocd-deploy.py ui [--context <ctx>] [--port 8080]
```

1. Starts `kubectl port-forward svc/argocd-server -n argocd <port>:80` in the background.
2. Prints URL (`http://localhost:<port>`) and decoded admin password.
3. Blocks until Ctrl+C, then terminates the port-forward process and exits cleanly.

### `help`

```
python scripts/argocd-deploy.py help
python scripts/argocd-deploy.py help <command>
python scripts/argocd-deploy.py --help
python scripts/argocd-deploy.py <command> --help
```

Top-level `help` prints a summary table of all sub-commands. `help <command>` prints full usage, all flags with defaults, and a concrete example. Standard argparse `--help` / `-h` works on every sub-command.

---

## Architecture

### File

`scripts/argocd-deploy.py` — single file, stdlib + subprocess only. No third-party dependencies.

### Shared helpers (internal to the file)

- `run(cmd, context=None, cwd=None)` — runs a subprocess, exits on non-zero. Prepends `--context <ctx>` for kubectl commands when context is provided.
- `kubectl(*args, context=None)` — thin wrapper around `run` for kubectl calls.
- `poll_applications(services, context, timeout)` — polls Application sync/health status until all pass or timeout.
- `write_tag(values_file, tag)` — reuses the same regex pattern from `rollback.py`.
- `git_commit_and_push(file, message)` — reuses the same pattern from `rollback.py`.
- `argo_password(context)` — base64-decodes `argocd-initial-admin-secret` (same as `stack-up.py`).

### Service discovery

Services are discovered by listing directories under `infra/gitops/envs/<env>/`. No hardcoded list. Works automatically when a new service is onboarded.

### Application naming convention

ArgoCD Applications are named `<service>` for local and `<service>-<env>` for staging/prod:

| Env | Application name | Manifest |
|---|---|---|
| `local` | `weather` | `infra/gitops/argo/weather.yaml` |
| `staging` | `weather-staging` | `infra/gitops/argo/weather-staging.yaml` |
| `prod` | `weather-prod` | `infra/gitops/argo/weather-prod.yaml` |

The `promote` command creates the target env Application manifest if it does not exist (copying from the local manifest and updating the `valueFiles` path to `envs/<to>/`), commits it alongside the image tag, and pushes. ArgoCD's app-of-apps picks it up automatically since it watches `infra/gitops/argo/` recursively.

### Environment-aware ArgoCD values

| Env | Values file used |
|---|---|
| `local` | `infra/argo/values.yaml` (insecure, low-resource) |
| `staging` | `infra/argo/values-staging.yaml` if exists, else `values.yaml` |
| `prod` | `infra/argo/values-prod.yaml` if exists, else `values.yaml` |

This is the extension point for future AWS-specific config (TLS enabled, LoadBalancer service type, larger resource limits) without script changes.

### `--context` propagation

All `kubectl` calls receive `--context <ctx>` when `--context` is provided. Helmfile calls receive `--kube-context <ctx>`. When omitted, the active kubectl context is used.

---

## AWS readiness

No cluster provisioning. For staging/prod on EKS:

```
python scripts/argocd-deploy.py install --env prod --context my-eks-context
python scripts/argocd-deploy.py promote weather --from staging --to prod
python scripts/argocd-deploy.py verify --env prod --context my-eks-context
```

The script is context-agnostic. AWS-specific ArgoCD configuration (TLS, ingress, resource sizing) lives in `infra/argo/values-prod.yaml` — not in the script.

---

## README updates

1. **New subsection under local stack documentation** — covers the five sub-commands with a typical first-run sequence and a post-merge watch workflow.
2. **One-line addition to the Rollback section** — notes that `verify` confirms a rollback landed.

---

## Typical workflows

**First time local setup:**
```
python scripts/argocd-deploy.py install --env local
python scripts/argocd-deploy.py ui
```

**After a merge to main (watching CI-triggered rollout):**
```
python scripts/argocd-deploy.py watch weather --env local
```

**Promoting a tested build to staging:**
```
python scripts/argocd-deploy.py promote weather --from local --to staging
python scripts/argocd-deploy.py verify --env staging --context my-staging-context
```

**Confirming a rollback landed:**
```
python scripts/argocd-deploy.py verify --env local
```

---

## Out of scope

- Progressive delivery / canary (Argo Rollouts) — future phase
- Cluster provisioning (EKS, Kind) — owned by other scripts
- Observability stack deployment — owned by `stack-up.py` / future refactor
- Removing ArgoCD from `stack-up.py` — separate branch
