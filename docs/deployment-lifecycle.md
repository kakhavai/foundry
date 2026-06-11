# Deployment Lifecycle

How a code change goes from a developer's machine to running in the cluster.

---

## The Full Flow

```
git push -> PR opened
  └── CI: lint / test / helm-lint  (on every push)
  └── CI: integration-test  (path-filtered; on every push to the PR)
        - changes job inspects the PR diff
        - Skips if the diff does not touch services/, helm/, infra/, scripts/
        - Otherwise, on the deployable surface:
            - Kind cluster created
            - Observability stack deployed
            - Services deployed via Helm
            - All endpoints smoke-tested
            - Cluster torn down
        - A new push cancels the PR's prior in-flight run (concurrency)
      -> required status check passes -> merge button unlocks

Merge to main
  └── CI: build-push
        - Docker image built
        - Image pushed to GHCR as ghcr.io/kakhavai/foundry/<service>:<sha>
  └── CI: update-gitops-tag (runs after build-push)
        - infra/gitops/envs/local/<service>/values.yaml updated with new SHA
        - Commit: "chore(gitops): update <service> image tag to <sha>"
        - Pushed to main

Argo CD reconciliation (automatic, within ~30s of the tag commit)
  └── Argo CD detects change in infra/gitops/
  └── Computes diff: current tag vs desired tag
  └── Initiates rolling update on the Deployment
  └── New pod starts, passes liveness probe
  └── Old pod terminated
  └── Application status: Synced + Healthy
```

---

## Observing a Deploy

**Argo CD UI** (http://localhost:8080 when running locally):
- Application card shows `Syncing` -> `Synced` with a green heart
- Click the Application to see the resource graph: Deployment, ReplicaSet, Pods
- History tab shows the git commit that triggered the sync

**Grafana** (http://localhost:3000):
- Check the service dashboard for request rate and error rate post-deploy
- Any spike in errors immediately after the sync timestamp warrants investigation

---

## Rollback

See [docs/runbooks/rollback.md](runbooks/rollback.md).

---

## Key Files

| File | Role |
|---|---|
| `infra/gitops/envs/local/<svc>/values.yaml` | Live desired image tag — updated by CI |
| `infra/gitops/argo/<svc>.yaml` | Argo CD Application definition |
| `helm/values/<svc>/values.yaml` | Stable service config (port, resources, image repo) |
| `helm/charts/generic-service/` | Shared Helm chart template |
| `.github/actions/update-gitops-tag/action.yml` | CI action that commits the new tag |
| `.github/workflows/integration-test.yml` | Path-filtered PR gate (runs the Kind test on deployable-surface changes) |
