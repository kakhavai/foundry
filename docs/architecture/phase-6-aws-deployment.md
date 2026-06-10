# Phase 6 — AWS Deployment

**Goal:** Lift the Foundry platform from local Kind to production AWS. The Kubernetes workload model, GitOps patterns, observability stack, and CI pipeline established in Phases 1–3 carry forward unchanged. Phase 6 adds the AWS infrastructure layer underneath them and defines the delta that any new service needs to be live on AWS beyond what Phase 2 already covers.

---

## Scope

**In scope:**
- EKS cluster provisioned via Terraform (VPC, node groups, IAM, OIDC provider)
- AWS Load Balancer Controller + ACM + Route 53 for ingress and TLS
- IRSA for pod-level IAM; GitHub Actions OIDC federation for CI (no stored credentials anywhere)
- ECR as the container registry (replaces GHCR)
- Full LGTM observability stack running on EKS with EBS-backed persistent storage
- ArgoCD running on EKS, tracking `main` via the existing app-of-apps pattern from Phase 3
- Per-service AWS delta documented — what changes beyond the Phase 2 golden path
- Terraform module structure designed to support workspace-based ephemeral environments (full CI wiring deferred to a later phase)

**Out of scope:**
- Multi-account AWS setup
- Ephemeral environment CI automation and environment promotion runbook (planned for a later phase)
- `player-data` service buildout
- Multi-region deployment

**Design principle:** A service that works locally after following Phase 2 should need only the additions described in the Per-Service Delta section to be live on AWS. Phase 2 and Phase 6 docs are the complete onboarding path.

---

## Architecture

### What changes

| Layer | Before (Phase 2) | After (Phase 6) |
|---|---|---|
| Cluster | Kind (local) | EKS (AWS) |
| Registry | GHCR | ECR |
| Ingress | `kubectl port-forward` | ALB + Route 53 + ACM TLS |
| Pod IAM | None | IRSA per service |
| CI auth | GitHub token | OIDC → IAM role (no stored secrets) |
| Secrets | Manually-created K8s Secrets | External Secrets Operator → AWS Secrets Manager |
| Storage | Ephemeral (local) | EBS-backed PersistentVolumes |

### What stays the same

- Service code, Dockerfiles, `pyproject.toml`, `uv.lock`
- Helm charts (`helm/charts/generic-service/`) and per-service value overrides (`helm/values/`)
- GitOps manifests in `infra/gitops/` — ArgoCD still tracks `main`
- LGTM observability Helmfile in `infra/grafana-stack/`
- CI jobs: lint, test, helm-lint, integration-test, `ready-for-merge` label gate
- App-of-apps ArgoCD pattern from Phase 3

### Terraform layout

```
infra/
  terraform/
    modules/
      vpc/          # VPC, subnets, NAT gateway
      eks/          # EKS cluster, managed node groups, OIDC provider
      iam/          # IRSA roles per service, GitHub Actions OIDC role, node ECR pull policy
      ecr/          # ECR repositories, one per service
      alb/          # AWS Load Balancer Controller IAM policy
      dns/          # Route 53 hosted zone, ExternalDNS IAM
    envs/
      prod/         # calls modules, owns tfvars, remote state backend config
```

Modules are intentionally parameterized so a future phase can add `envs/dev/` or workspace-based PR environments without restructuring. That CI wiring and the environment promotion runbook are out of scope for Phase 6.

**Remote state:** S3 bucket + DynamoDB lock table, bootstrapped once manually before any `terraform apply`. Config lives in `envs/prod/backend.tf`.

### Cluster-level components (bootstrapped by ArgoCD, not Terraform)

These run on the EKS cluster and are managed via GitOps like every other workload:

- **AWS Load Balancer Controller** — reads `Ingress` objects, provisions ALBs
- **ExternalDNS** — watches `Ingress` objects, writes Route 53 records automatically
- **cert-manager** — provisions and rotates TLS certs via ACM annotations
- **External Secrets Operator** — syncs AWS Secrets Manager → Kubernetes Secrets

---

## Per-Service AWS Delta

A service that follows the Phase 2 golden path needs at most three additions to run on EKS.

### 1. Ingress annotations (`helm/values/<name>/values.yaml`)

```yaml
ingress:
  enabled: true
  annotations:
    kubernetes.io/ingress.class: alb
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/certificate-arn: <ACM cert ARN>
  host: <name>.foundry.yourdomain.com
```

ExternalDNS picks up the host and writes the Route 53 record automatically. ACM handles TLS rotation. No manual DNS or certificate management.

### 2. IRSA annotation on the ServiceAccount (only if the service needs AWS access)

```yaml
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::<account>:role/foundry-<name>
```

Services with no AWS dependencies (e.g. `weather`) skip this entirely.

### 3. Secrets via External Secrets Operator (only if the service needs secrets)

```yaml
# infra/gitops/envs/prod/<name>/externalsecret.yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
spec:
  secretStoreRef:
    name: aws-secrets-manager
  target:
    name: <name>-secrets
  data:
    - secretKey: MY_SECRET
      remoteRef:
        key: foundry/<name>/my-secret
```

The existing `extraEnv` + `secretKeyRef` pattern in the Helm chart stays unchanged — only the Secret source changes from manually-created to ESO-managed.

### ECR repository

Terraform provisions one ECR repository per service in `infra/terraform/modules/ecr/`. No per-service Terraform changes are needed for new services — the ECR module accepts a list of service names.

---

## CI Changes

### Terraform plan/apply (`infra/terraform/`)

A new workflow `.github/workflows/terraform.yml` triggers on changes to `infra/terraform/**`:

- **On PR:** runs `terraform plan`, posts the plan output as a PR comment
- **On merge to main:** runs `terraform apply`, gated by a GitHub environment protection rule (`production`) requiring manual approval before apply

Auth uses OIDC — no stored AWS credentials:

```yaml
- uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: arn:aws:iam::<account>:role/foundry-github-actions
    aws-region: us-east-1
```

### build-push migrates to ECR

The existing `build-push` composite action gains ECR login and updates the image URI:

```yaml
- uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: arn:aws:iam::<account>:role/foundry-github-actions
    aws-region: us-east-1

- uses: aws-actions/amazon-ecr-login@v2

- name: Build and push
  run: |
    docker build -t <account>.dkr.ecr.us-east-1.amazonaws.com/foundry/<service>:<sha> .
    docker push <account>.dkr.ecr.us-east-1.amazonaws.com/foundry/<service>:<sha>
```

EKS node groups receive `AmazonEC2ContainerRegistryReadOnly` on their IAM role — pods pull images without `imagePullSecrets`.

### GitOps tag update

Unchanged in behavior — CI writes the new ECR image tag to `infra/gitops/` after a successful push. Only the image URI format changes.

### Unchanged

Lint, test, helm-lint, integration-test, and the `ready-for-merge` label gate are untouched.

---

## Milestones

### Stage 1 — AWS Foundation

Terraform provisions the AWS baseline. Nothing runs on it yet.

- [ ] Remote state bootstrapped: S3 bucket + DynamoDB lock table created manually
- [ ] `infra/terraform/modules/` — vpc, eks, iam, ecr, alb, dns modules written
- [ ] `infra/terraform/envs/prod/` — production env wired to modules, tfvars set
- [ ] GitHub Actions OIDC IAM role created (trust policy scoped to this repo)
- [ ] `terraform apply` produces a working EKS cluster
- [ ] `.github/workflows/terraform.yml` — plan-on-PR and apply-on-merge working with OIDC auth
- [ ] ECR repositories provisioned for `weather` and `player-projections`

### Stage 2 — Platform Bootstrap

Cluster-level components and GitOps layer come up on EKS.

- [ ] AWS Load Balancer Controller deployed via ArgoCD
- [ ] ExternalDNS deployed via ArgoCD, Route 53 writes verified
- [ ] cert-manager deployed via ArgoCD, ACM cert provisioning verified
- [ ] External Secrets Operator deployed via ArgoCD
- [ ] ArgoCD running on EKS, app-of-apps reconciling `main`
- [ ] LGTM observability stack running with EBS-backed PersistentVolumes
- [ ] Grafana reachable via public ALB URL with TLS

### Stage 3 — Service Migration

Existing services go live on EKS end-to-end.

- [ ] `build-push` composite action migrated to ECR
- [ ] `weather` deployed via GitOps, reachable at `weather.foundry.<domain>` with TLS
- [ ] `player-projections` deployed via GitOps, reachable at `player-projections.foundry.<domain>` with TLS
- [ ] Per-service delta validated: onboard a new service from scratch using Phase 2 + Phase 6 docs only

---

## Ephemeral Environments (Planned)

The Terraform module structure above is intentionally parameterized to support workspace-based ephemeral environments in a future phase. The pattern:

- `terraform workspace new pr-<number>` spins up a full isolated AWS environment (VPC, EKS, everything) with its own state
- CI applies it on PR open, smoke tests it, destroys it on PR close
- Long-lived non-production environments follow the `infra/terraform/envs/dev/` pattern

The full CI wiring, environment promotion runbook, and cost controls for ephemeral environments are deferred. The Phase 6 module structure does not need to change to support them.

---

## Design Decisions

**Why Terraform in `infra/terraform/`, not a separate repo.**
The repo already treats `infra/` as first-class: `infra/gitops/`, `infra/grafana-stack/`, `infra/kind/`. Terraform belongs in the same tier. A separate repo is the right call at org scale with dedicated platform teams and strict access separation — not here. Keeping it together lets a single PR change a Helm value and the Terraform variable that feeds it.

**Why ALB over NGINX Ingress.**
ALB is the AWS-native standard for EKS. It integrates directly with ACM (free TLS certs), Route 53 (via ExternalDNS), and IAM. NGINX is the right call for cloud-agnostic portability; this phase is explicitly AWS-native.

**Why IRSA + OIDC over stored credentials.**
No secrets stored anywhere — not in GitHub, not in Kubernetes, not in the repo. IRSA scopes IAM permissions to individual service accounts. OIDC scopes CI permissions to this specific repo and branch. This is the production-grade posture the platform is demonstrating.

**Why ECR over GHCR.**
ECR integrates natively with EKS via the node IAM role — no `imagePullSecrets` required. Images stay within the AWS network boundary. The switch is straightforward: one composite action change, one image URI format change.

**Why cluster-level components are managed by ArgoCD, not Terraform.**
Terraform owns AWS infrastructure. ArgoCD owns Kubernetes resources. Mixing them via the Terraform Helm/Kubernetes providers blurs that boundary and makes cluster state harder to reason about. The ALB controller, ExternalDNS, and cert-manager are Kubernetes workloads — they belong in GitOps.
