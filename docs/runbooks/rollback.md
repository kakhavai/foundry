# Rollback Runbook

Roll back a service to a previous image tag when a bad deploy is detected.

---

## When to Roll Back

Roll back when, after a deploy:
- `/health` returns non-200 for more than 60 seconds
- Error rate in Grafana spikes above baseline immediately post-deploy
- A critical feature is broken that was working before the deploy
- Argo CD reports the Application as `Degraded`

Do NOT roll back for:
- Flapping that resolves within 2 minutes (liveness probe restart may fix it)
- Issues unrelated to the most recent deploy

---

## Find the Target Tag

**Option 1 — Argo CD sync history (easiest)**
Open http://localhost:8080 -> select the service Application -> History tab.
Each entry shows the git SHA that triggered the sync. Pick the last healthy one.

**Option 2 — Git log**
```bash
git log --oneline infra/gitops/envs/local/<service>/values.yaml
```
Shows every tag commit for that service. Find the last known-good SHA.

**Option 3 — GHCR**
Browse `https://github.com/kakhavai/foundry/pkgs/container/<service>` for available tags.

---

## Execute the Rollback

```bash
python scripts/rollback.py <service> <target-tag>

# Example:
python scripts/rollback.py weather abc1234
```

The script:
1. Validates the service name
2. Writes the target tag to `infra/gitops/envs/local/<service>/values.yaml`
3. Commits: `revert(<service>): roll back to <target-tag>`
4. Pushes to main
5. Prints verification steps

---

## Verify the Rollback

1. **Argo CD UI** (http://localhost:8080): Application goes `OutOfSync -> Syncing -> Synced+Healthy`. Takes 30-90 seconds.

2. **Check running image:**
   ```bash
   kubectl get deployment <service> \
     -o jsonpath='{.spec.template.spec.containers[0].image}'
   ```
   Expected: `ghcr.io/kakhavai/foundry/<service>:<target-tag>`

3. **Health check:**
   ```bash
   curl http://localhost:<port>/health
   ```
   Expected: `{"status": "ok"}`

4. **Metrics** (check in Grafana): error rate should return to baseline.

---

## If the Rollback Fails

If the rolled-back version is also unhealthy:
1. Check `kubectl logs -l app.kubernetes.io/name=<service> --tail=50` for crash details
2. Find an earlier known-good tag via git log and roll back again
3. If no known-good tag: roll back to `0.1.0` (the initial chart appVersion)
4. Escalate: open an incident, do not leave the service in a degraded state
