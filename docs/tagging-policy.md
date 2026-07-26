# Tagging Policy

How Foundry uses git tags, and how deploy identity works. Two different jobs that
both happen to use the word "tag" — keep them separate.

---

## TL;DR

- **Milestone tags** (`phase-N`) mark when the platform reached a capability. Annotated, ~7 total, documentary.
- **GitOps deploys** run on **immutable Git-SHA image tags** — unchanged by any of this.
- **SemVer release tags** are **deferred** until a service has an external consumer that pins versions.
- **Enforcement is convention**, not CI: a Definition-of-Done checklist + a PR-template line.

---

## 1. Milestone tags — `phase-N`

Each phase in the [roadmap](../README.md#phases) gets one annotated tag when the
phase reaches its **Definition of Done** (its milestones in
`docs/architecture/phase-N-*.md` are all checked and merged to `main`).

**Convention**

- Name: `phase-N` (e.g. `phase-4`) — matches the docs' "Phase N" language.
- **Annotated only** (`git tag -a`), never lightweight. Annotated tags carry an
  author, date, and message, and are the only kind meant to be pushed/shared.
- The message cites the delivering PR **and** the deployed image SHA, so the
  roadmap marker links straight into the GitOps deploy that proved it.

**Creating one**

```bash
git tag -a phase-5 <merge-commit> -m "Phase 5 complete — Resilience + AI adversarial layer

Landed in #NN.
Deployed image: <sha>."
git push origin phase-5
```

**Current tags**

| Tag | Commit | Landed |
|---|---|---|
| `phase-1` | `e697160` | #10 |
| `phase-2` | `91c1d13` | #12 |
| `phase-3` | `b68a58f` | GitOps deployment |
| `phase-4` | `f786593` | #36 |

---

## 2. Deploy identity — Git-SHA image tags (GitOps)

This is **not** changing. CI builds each image tagged with the **full Git SHA** of
the commit that produced it, then writes that SHA into
`infra/gitops/envs/<env>/<service>/values.yaml`. Argo CD reconciles.

Why SHA tags rather than SemVer for deploys:

- **Immutable.** A SHA can never be re-pointed the way a moving `v1.2.0` or
  `latest` tag can. GitOps depends on that.
- **Traceable.** Every running pod maps back to exactly one commit for free.
- **GitOps-native.** Both Argo CD and Flux steer toward it.

Milestone tags (§1) point at **app-repo merge commits**. They never appear in
`infra/gitops/` and never drive a deploy. The two systems are orthogonal.

---

## 3. SemVer release tags — deferred (graduation trigger)

Full artifact versioning (`svc/vMAJOR.MINOR.PATCH`, e.g. `weather/v0.3.1`) is the
industry standard **once you have consumers pinning versions**. Foundry has none
yet, so adopting it now would add a versioning workflow and a sync surface with no
payoff.

**Graduation trigger:** the day a service gains an external consumer that pins a
version, **that service** — not the whole repo — adopts per-service
`svc/vX.Y.Z` annotated tags. Monorepo rule: **no single repo-wide version number**,
because it lies about services that didn't change (this is why Go modules, Lerna,
and changesets all use path-prefixed tags).

Until then: SHA-based GitOps is the only versioning the platform needs.

---

## 4. Enforcement

Deliberately lightweight — a milestone tag happens a handful of times, so a CI gate
would be over-engineering.

- **Definition of Done:** each phase doc ends with a checklist whose final item is
  "tag the milestone commit and push."
- **PR template:** `.github/PULL_REQUEST_TEMPLATE.md` reminds you to tag when a PR
  completes a phase.
- **No CI automation.** Revisit only if tag cadence ever becomes high-frequency
  (i.e. once §3's SemVer graduation happens for real).
