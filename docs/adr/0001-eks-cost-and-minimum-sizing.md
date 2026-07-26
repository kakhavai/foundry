# ADR 0001 — EKS Cost Analysis and Minimum Sizing

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Platform owner (kakhavai)
- **Supersedes sizing in:** `docs/plans/2026-06-10-phase-6-aws-deployment.md` (node group defaults)

---

## Context and Problem Statement

Phase 6 lifts Foundry from local Kind to AWS EKS. The committed Phase 6 plan defaults
to a production-shaped node group (`2× t3.large`, `min_size = 2`, single NAT). For this
project the goal is **learning to deploy and operate EKS**, not serving production scale
traffic. Before spending real money we need an honest answer to a single question:

> What does it cost to run Foundry on EKS at the smallest footprint that still works,
> operated always-on, and how do we keep the door open to scale up later?

This ADR records the cost analysis and the sizing decision that follows from it.

## Decision Drivers

- **Learn EKS properly** — IRSA, ALB Controller, ACM/Route53/ExternalDNS, GitHub OIDC
  federation, Terraform-managed VPC/EKS/ECR/IAM, ArgoCD on real cloud, cluster day-2 ops.
- **Minimize cost** — no scaling workloads are expected; pay for learning, not throughput.
- **Always-on** — operate a continuously-running system (upgrades, cert renewals, drift),
  not an ephemeral spin-up/tear-down. This is a deliberate choice for day-2 realism.
- **Scale-up must be cheap to reach** — starting small must not paint us into a corner;
  growing should be a one-variable Terraform change, not a redesign.

## What "minimum" does and does not teach

Minimum sizing is a cost decision, **not** a learning limitation. Everything EKS-specific
and resume-valuable runs on a single small node:

| Learned at minimum (single node) | Requires scale-up (deferred) |
|---|---|
| IRSA (IAM roles for service accounts) | Multi-node scheduling / pod anti-affinity |
| AWS Load Balancer Controller + ACM + Route53 + ExternalDNS | Surviving a node failure (workload HA) |
| GitHub Actions OIDC → IAM role federation | Cluster Autoscaler / Karpenter *in action* |
| Terraform: VPC, EKS, ECR, IAM, OIDC provider | Rolling node replacement across a fleet |
| ArgoCD on real cloud, cluster upgrades, `kubectl` day-2 ops | |
| cert-manager / TLS issuance, ECR pull via node role | |

The right column is add-later scale topics, not fundamentals. **The EKS control plane
itself is already multi-AZ and highly available regardless of node count** — that HA is
baked into the flat control-plane fee, so a single node still runs against a real HA
control plane.

---

## Minimum Footprint (the decision)

Region **`us-east-1`** (cheapest; matches the Phase 6 plan). On-demand pricing.

| Component | Choice | Rationale |
|---|---|---|
| Control plane | 1 EKS cluster | Unavoidable fixed cost; multi-AZ HA included |
| Node group | **1× `t4g.large`** (Graviton, 2 vCPU / 8 GiB), `desired = 1`, `min = 1`, **`max = 3`** | Fits the full LGTM stack + ArgoCD + 3 services; Graviton is ~10–15% cheaper than x86 `t3.large` and Foundry's Python images run on ARM. `max = 3` leaves autoscaling headroom already wired. |
| Networking | **Single NAT gateway** (private node subnets) | Production-correct default. Can drop to public subnets (no NAT) to save ~$34/mo — see levers. |
| Ingress | **1 shared ALB** via ingress group | One ALB fronts all services instead of one per service. |
| TLS / DNS | ACM (free certs) + Route53 + ExternalDNS + cert-manager | Standard AWS-native ingress path. |
| Observability storage | **Loki + Tempo → S3** (not EBS PVCs) | Currently `filesystem` in `infra/grafana-stack/values/`. On EKS, point them at S3 — cheaper, survives node replacement, decouples telemetry from the node. Prometheus + Grafana keep small EBS PVCs. |
| Registry | ECR (one repo per service) | Node IAM role pulls images; no `imagePullSecrets`. |

**Node fit note:** the LGTM stack (Loki SingleBinary, Tempo, Prometheus, Grafana, OTel
Collector), ArgoCD, AWS LB Controller, ExternalDNS, cert-manager, and the three app
services (each requesting `100m` CPU / `128Mi`) total ~30 pods. This fits an 8 GiB node
but is not roomy. If scheduling pressure appears, `t4g.xlarge` (16 GiB) is a one-line
change — see scale-up path.

---

## Cost Breakdown — Always-On, Minimum (monthly, us-east-1)

| Line item | Unit cost | Monthly |
|---|---|---:|
| EKS control plane | $0.10/hr × 730 | **$73.00** |
| 1× `t4g.large` node (on-demand) | $0.0672/hr × 730 | **$49.06** |
| NAT gateway (single) | $0.045/hr × 730 + ~$1 data | **$33.85** |
| Application Load Balancer | $0.0225/hr × 730 + ~$2 LCU | **$18.00** |
| EBS gp3 (~50 GiB: root + Prometheus/Grafana PVCs) | $0.08/GiB | **$4.00** |
| S3 (Loki/Tempo chunks + Terraform state) | few GiB | **$1.00** |
| Route53 hosted zone | $0.50 + negligible queries | **$0.50** |
| ECR storage | ~1–2 GiB | **$1.00** |
| Data transfer out (light) | | **$2.00** |
| EKS control-plane CloudWatch logs (optional) | can disable | **$0–3.00** |
| **Total (with NAT)** | | **≈ $182 – $185/mo** |
| **Total (no NAT, public node subnets)** | | **≈ $148/mo** |

> **The irreducible floor is ~$73/mo** — the control plane bills 24/7 from the moment the
> cluster exists, even with zero nodes and nothing deployed. Everything else is optional
> or reducible.

### How this compares

| Option | Monthly | Trade-off |
|---|---:|---|
| Local Kind (today) | **$0** | No real cloud, no public URL, no IRSA/OIDC/ALB learning |
| **EKS minimum, always-on (this ADR)** | **~$150–185** | Real cloud + day-2 ops; single node = no workload HA |
| EKS minimum, spot node + no NAT | **~$110** | Aggressive floor; spot interruptions reschedule workloads |
| EKS Phase-6 default (`2× t3.large`, NAT) | **~$254** | HA scheduling; overkill for "no scaling needed" |
| EKS ephemeral (up for sessions, destroyed) | **~$10–20** | Cheapest, but loses always-on day-2 realism |

---

## Cost Levers

Ordered by savings, all reversible with a variable change:

1. **Spot node** — `t4g.large` spot ≈ $0.02/hr ≈ **~$15/mo** (saves ~$34). A single spot
   node means an interruption reschedules the whole workload — acceptable and educational
   for a learning cluster; Spot handling is itself a worthwhile topic.
2. **Drop NAT gateway** — put nodes in public subnets: **saves ~$34/mo**. Slightly less
   production-correct; fine for a learning cluster with no private-egress requirement.
3. **1-year Compute Savings Plan on the node** — ~40% off the node: **saves ~$20/mo**.
   Only worth it once you've committed to keeping the cluster up for months.
4. **Disable EKS control-plane CloudWatch logging** — **saves $0–3/mo**; re-enable when
   debugging the API server.
5. **S3-backed Loki/Tempo** (already in the footprint) — keeps EBS spend near zero and
   makes the node stateless.

**Rejected:** Fargate. Per-pod pricing across ~30 pods exceeds a single shared node, and
Fargate can't run DaemonSet-style workloads the observability stack expects.

**Not the bottleneck:** the ~$73 control-plane fee is fixed. No lever removes it short of
not running EKS at all (i.e. going ephemeral or staying on Kind).

---

## Scale-Up Path (deferred, cheap to reach)

Every scale action is a Terraform variable change + `apply`. Nothing here requires a
redesign — this is why starting minimal costs nothing in flexibility.

| Goal | Change | Added cost/mo |
|---|---|---:|
| Workload HA (survive node loss) | `desired_size = 2` | +$49 |
| More scheduling capacity | `instance_types = ["t4g.xlarge"]` | +$49/node |
| Autoscaling in action | Already have `max_size = 3`; add Cluster Autoscaler/Karpenter | ~$0 idle |
| NAT HA across AZs | second/third NAT gateway | +$34 each |

---

## Consequences

**Positive**
- ~$150–185/mo buys the full EKS-specific skill set plus always-on day-2 operation.
- Single node keeps burn near the irreducible control-plane floor.
- Scale-up is a one-variable change; no lock-in from starting small.
- S3-backed telemetry means node replacement doesn't lose observability data.

**Negative / accepted risks**
- **No workload HA** at `desired = 1`: a node replacement or Kubernetes version upgrade
  causes downtime. Acceptable — there is no SLA, and experiencing that downtime is itself
  a lesson in *why* HA matters.
- **Always-on burn** continues whether or not the cluster is actively used. If usage
  becomes sporadic, revisit the ephemeral operating model (~$10–20/mo).
- **8 GiB node is tight** for the full stack; may need `t4g.xlarge` (+$49/mo) if pods
  fail to schedule.

**Billing gotchas to watch**
- Control plane bills from cluster creation, not from first deploy — **destroy the cluster,
  not just the workloads, to stop the ~$73/mo.**
- NAT and ALB bill hourly while they exist, idle or not.
- `terraform destroy` can leave **orphaned** ALBs/EBS volumes/EIPs created by in-cluster
  controllers (AWS LB Controller, PVCs) — verify the AWS console is clean after teardown.
- NAT **data processing** ($0.045/GiB) can surprise if images are pulled repeatedly over
  it; ECR is in-region, and VPC endpoints for ECR/S3 avoid NAT egress for pulls.

---

## Decision

Adopt EKS with an **always-on minimum footprint**: a single `t4g.large` node
(`desired = 1`, `max = 3`), single NAT gateway, one shared ALB, and S3-backed Loki/Tempo,
in `us-east-1`. Budget **~$150–185/mo**, with documented levers (spot, no-NAT) that reach
**~$110/mo** and a one-variable scale-up path to HA when wanted.

This **revises the Phase 6 plan's node-group default** (`2× t3.large`, `min_size = 2`) to
the minimum sizing above. The Phase 6 implementation plan should be updated to match
before Stage 1 apply.
