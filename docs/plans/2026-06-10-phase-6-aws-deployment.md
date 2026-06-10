# Phase 6 — AWS Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lift the Foundry platform from local Kind to production AWS EKS — Terraform provisions the AWS foundation, ArgoCD bootstraps the cluster-level platform components, and the existing services migrate to run on EKS reachable over public ALBs with TLS.

**Architecture:** Terraform (in `infra/terraform/`) owns all AWS infrastructure — VPC, EKS, IAM, ECR, Route 53. ArgoCD owns everything inside Kubernetes, including the platform add-ons (AWS Load Balancer Controller, ExternalDNS, cert-manager, External Secrets Operator) and the existing services, via the Phase 3 app-of-apps pattern. CI uses GitHub Actions OIDC to assume an IAM role — no stored credentials. The boundary is strict: Terraform stops at the cluster edge; ArgoCD takes over inside it.

**Tech Stack:** Terraform >= 1.9, terraform-aws-modules (vpc ~> 5.0, eks ~> 20.0), AWS provider ~> 5.0, EKS 1.30, Helm 3.16, ArgoCD (chart 7.6.0), AWS Load Balancer Controller, ExternalDNS, cert-manager, External Secrets Operator, ECR, GitHub Actions OIDC.

**Reference spec:** `docs/architecture/phase-6-aws-deployment.md`

---

## Execution model for infrastructure work

This plan provisions cloud infrastructure, so the verification model differs from application TDD:

- **"Failing test" → `terraform validate` / `terraform plan`** showing the resource is not yet present, or `helm template | grep` showing a manifest field is absent.
- **"Passing test" → `terraform plan` shows the intended diff**, `terraform apply` succeeds, or `kubectl`/`helm template` confirms the live/rendered state.
- **Apply is gated.** `terraform apply` against real AWS costs money and is destructive. Stage 1 applies happen behind the `production` GitHub environment approval gate (Task 8) or manually by the operator. Never script an unattended apply.
- **Helm chart changes (Stage 3) DO get real assertion tests** via `helm template` output checks — those follow standard TDD.

Each stage leaves the platform in a working, verifiable state.

---

## Prerequisites (manual, one-time — not automated by this plan)

These are operator actions the plan assumes are already done before Task 1:

1. An AWS account exists and the operator has admin credentials locally (`aws sts get-caller-identity` succeeds).
2. A registered domain with a Route 53 **public hosted zone** (e.g. `foundry.example.com`). Record its hosted zone ID and name.
3. Terraform >= 1.9 and AWS CLI v2 installed locally.
4. Decide the AWS region once (this plan uses `us-east-1` throughout — change in one place: `infra/terraform/envs/prod/terraform.tfvars`).

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `infra/terraform/bootstrap/main.tf` | Create | S3 state bucket + DynamoDB lock table (one-time, local state) |
| `infra/terraform/bootstrap/README.md` | Create | How to run the one-time bootstrap |
| `infra/terraform/envs/prod/backend.tf` | Create | S3 remote state backend config |
| `infra/terraform/envs/prod/providers.tf` | Create | AWS + Kubernetes provider config |
| `infra/terraform/envs/prod/variables.tf` | Create | Input variables for the prod env |
| `infra/terraform/envs/prod/terraform.tfvars` | Create | Concrete values (region, domain, account-specific) |
| `infra/terraform/envs/prod/main.tf` | Create | Wires modules together |
| `infra/terraform/envs/prod/outputs.tf` | Create | Cluster name, ECR URLs, role ARNs |
| `infra/terraform/modules/vpc/main.tf` | Create | VPC via terraform-aws-modules |
| `infra/terraform/modules/vpc/variables.tf` | Create | VPC inputs |
| `infra/terraform/modules/vpc/outputs.tf` | Create | VPC/subnet IDs |
| `infra/terraform/modules/eks/main.tf` | Create | EKS cluster + managed node group + OIDC provider |
| `infra/terraform/modules/eks/variables.tf` | Create | EKS inputs |
| `infra/terraform/modules/eks/outputs.tf` | Create | Cluster endpoint, OIDC issuer, node role |
| `infra/terraform/modules/ecr/main.tf` | Create | One ECR repo per service name |
| `infra/terraform/modules/ecr/variables.tf` | Create | Service name list |
| `infra/terraform/modules/ecr/outputs.tf` | Create | Repo URLs map |
| `infra/terraform/modules/iam/github_oidc.tf` | Create | GitHub Actions OIDC provider + CI role |
| `infra/terraform/modules/iam/irsa.tf` | Create | IRSA roles for platform add-ons + services |
| `infra/terraform/modules/iam/variables.tf` | Create | IAM inputs (OIDC issuer, repo, namespaces) |
| `infra/terraform/modules/iam/outputs.tf` | Create | Role ARNs |
| `.github/workflows/terraform.yml` | Create | plan-on-PR, gated apply-on-merge (OIDC auth) |
| `infra/gitops/argo/platform/aws-load-balancer-controller.yaml` | Create | Argo Application for ALB controller |
| `infra/gitops/argo/platform/external-dns.yaml` | Create | Argo Application for ExternalDNS |
| `infra/gitops/argo/platform/cert-manager.yaml` | Create | Argo Application for cert-manager |
| `infra/gitops/argo/platform/external-secrets.yaml` | Create | Argo Application for External Secrets Operator |
| `infra/gitops/argo/platform/values/*.yaml` | Create | Helm values for each add-on |
| `infra/gitops/argo/app-of-apps.yaml` | Modify | Add the `platform/` directory to the app-of-apps source |
| `infra/gitops/envs/prod/weather/values.yaml` | Create | Prod image tag for weather |
| `infra/gitops/envs/prod/player-projections/values.yaml` | Create | Prod image tag for player-projections |
| `infra/gitops/argo/weather.yaml` | Modify | Point at prod overlay + ECR repo (or add prod Application) |
| `infra/gitops/argo/player-projections.yaml` | Modify | Point at prod overlay + ECR repo |
| `helm/charts/generic-service/templates/ingress.yaml` | Create | ALB Ingress template (gated on `ingress.enabled`) |
| `helm/charts/generic-service/templates/serviceaccount.yaml` | Create | ServiceAccount with optional IRSA annotation |
| `helm/charts/generic-service/templates/deployment.yaml` | Modify | Reference the named ServiceAccount |
| `helm/charts/generic-service/values.yaml` | Modify | Add `ingress` + `serviceAccount` default blocks |
| `helm/values/weather/values.yaml` | Modify | Enable ingress for weather |
| `helm/values/player-projections/values.yaml` | Modify | Enable ingress for player-projections |
| `.github/actions/build-push/action.yml` | Modify | Add ECR login, push to ECR instead of GHCR |
| `.github/actions/update-gitops-tag/action.yml` | Modify | Write to `envs/prod/` path |
| `.github/workflows/weather.yml` | Modify | OIDC auth + ECR image name |
| `.github/workflows/player-projections.yml` | Modify | OIDC auth + ECR image name |
| `CLAUDE.md` | Modify | Document AWS deploy model, ECR, IRSA, ingress delta |
| `docs/runbooks/aws-bootstrap.md` | Create | One-time AWS bring-up runbook |

---

# Stage 1 — AWS Foundation

Provision the AWS baseline with Terraform. End state: a working EKS cluster, ECR repos, IAM roles, reachable via `kubectl get nodes`. Nothing runs on the cluster yet.

---

### Task 1: One-time remote state backend

**Files:**
- Create: `infra/terraform/bootstrap/main.tf`
- Create: `infra/terraform/bootstrap/README.md`

The S3 bucket and DynamoDB lock table that hold all *other* state must themselves be created before remote state exists — so this module uses **local** state and is run once by hand. It is intentionally separate from `envs/prod`.

- [ ] **Step 1: Write the bootstrap module**

`infra/terraform/bootstrap/main.tf`:
```hcl
terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "state_bucket" {
  type    = string
  default = "foundry-terraform-state"
}

variable "lock_table" {
  type    = string
  default = "foundry-terraform-locks"
}

resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "locks" {
  name         = var.lock_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"
  attribute {
    name = "LockID"
    type = "S"
  }
}

output "state_bucket" { value = aws_s3_bucket.state.id }
output "lock_table"   { value = aws_dynamodb_table.locks.name }
```

- [ ] **Step 2: Write the bootstrap README**

`infra/terraform/bootstrap/README.md`:
```markdown
# Terraform State Backend Bootstrap

Run ONCE, by hand, before any other Terraform in this repo. Creates the S3
bucket and DynamoDB lock table that all other environments use as their
remote backend. Uses local state (committed nowhere — the `terraform.tfstate`
this produces can be discarded; the resources are named deterministically).

    cd infra/terraform/bootstrap
    terraform init
    terraform apply

If the bucket name `foundry-terraform-state` is taken (S3 names are global),
override it: `terraform apply -var state_bucket=foundry-tfstate-<unique>`
and update `infra/terraform/envs/prod/backend.tf` to match.
```

- [ ] **Step 3: Validate**

Run: `cd infra/terraform/bootstrap && terraform init && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add infra/terraform/bootstrap/
git commit -m "feat(terraform): add one-time remote state backend bootstrap"
```

---

### Task 2: prod environment root — backend, providers, variables

**Files:**
- Create: `infra/terraform/envs/prod/backend.tf`
- Create: `infra/terraform/envs/prod/providers.tf`
- Create: `infra/terraform/envs/prod/variables.tf`
- Create: `infra/terraform/envs/prod/terraform.tfvars`

- [ ] **Step 1: Write the backend config**

`infra/terraform/envs/prod/backend.tf`:
```hcl
terraform {
  required_version = ">= 1.9"
  backend "s3" {
    bucket         = "foundry-terraform-state"
    key            = "envs/prod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "foundry-terraform-locks"
    encrypt        = true
  }
}
```

- [ ] **Step 2: Write the variables**

`infra/terraform/envs/prod/variables.tf`:
```hcl
variable "region" {
  type        = string
  description = "AWS region for all resources"
}

variable "cluster_name" {
  type        = string
  description = "EKS cluster name"
}

variable "domain" {
  type        = string
  description = "Public domain managed in Route 53 (e.g. foundry.example.com)"
}

variable "hosted_zone_id" {
  type        = string
  description = "Route 53 public hosted zone ID for the domain"
}

variable "github_repo" {
  type        = string
  description = "owner/repo allowed to assume the CI role (e.g. kakhavai/foundry)"
}

variable "service_names" {
  type        = list(string)
  description = "Services that get an ECR repo"
  default     = ["weather", "player-projections"]
}
```

- [ ] **Step 3: Write concrete values**

`infra/terraform/envs/prod/terraform.tfvars`:
```hcl
region         = "us-east-1"
cluster_name   = "foundry-prod"
domain         = "foundry.example.com"     # CHANGE: your Route 53 domain
hosted_zone_id = "ZXXXXXXXXXXXXX"          # CHANGE: your hosted zone ID
github_repo    = "kakhavai/foundry"
service_names  = ["weather", "player-projections"]
```

- [ ] **Step 4: Write the providers**

`infra/terraform/envs/prod/providers.tf`:
```hcl
provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = "foundry"
      ManagedBy = "terraform"
      Env       = "prod"
    }
  }
}
```

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/envs/prod/backend.tf infra/terraform/envs/prod/providers.tf infra/terraform/envs/prod/variables.tf infra/terraform/envs/prod/terraform.tfvars
git commit -m "feat(terraform): scaffold prod env backend, providers, variables"
```

---

### Task 3: VPC module

**Files:**
- Create: `infra/terraform/modules/vpc/main.tf`
- Create: `infra/terraform/modules/vpc/variables.tf`
- Create: `infra/terraform/modules/vpc/outputs.tf`

Uses the canonical `terraform-aws-modules/vpc` rather than hand-rolling subnets. The subnet tags are required for the AWS Load Balancer Controller to discover where to place ALBs.

- [ ] **Step 1: Write the module**

`infra/terraform/modules/vpc/main.tf`:
```hcl
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${var.cluster_name}-vpc"
  cidr = "10.0.0.0/16"

  azs             = var.azs
  private_subnets = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
  public_subnets  = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]

  enable_nat_gateway   = true
  single_nat_gateway   = true
  enable_dns_hostnames = true

  # Required for AWS Load Balancer Controller subnet auto-discovery
  public_subnet_tags = {
    "kubernetes.io/role/elb"                      = "1"
    "kubernetes.io/cluster/${var.cluster_name}"   = "shared"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb"             = "1"
    "kubernetes.io/cluster/${var.cluster_name}"   = "shared"
  }
}
```

`infra/terraform/modules/vpc/variables.tf`:
```hcl
variable "cluster_name" { type = string }
variable "azs" {
  type    = list(string)
  default = ["us-east-1a", "us-east-1b", "us-east-1c"]
}
```

`infra/terraform/modules/vpc/outputs.tf`:
```hcl
output "vpc_id"          { value = module.vpc.vpc_id }
output "private_subnets" { value = module.vpc.private_subnets }
output "public_subnets"  { value = module.vpc.public_subnets }
```

- [ ] **Step 2: Validate (after wiring in Task 7; standalone init here)**

Run: `cd infra/terraform/modules/vpc && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add infra/terraform/modules/vpc/
git commit -m "feat(terraform): add VPC module with LB-controller subnet tags"
```

---

### Task 4: EKS module

**Files:**
- Create: `infra/terraform/modules/eks/main.tf`
- Create: `infra/terraform/modules/eks/variables.tf`
- Create: `infra/terraform/modules/eks/outputs.tf`

Uses `terraform-aws-modules/eks`. Enables the IRSA OIDC provider (`enable_irsa = true`) and grants the node group the ECR read policy so pods pull images without `imagePullSecrets`.

- [ ] **Step 1: Write the module**

`infra/terraform/modules/eks/main.tf`:
```hcl
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = var.cluster_name
  cluster_version = "1.30"

  cluster_endpoint_public_access = true
  enable_irsa                    = true

  vpc_id     = var.vpc_id
  subnet_ids = var.private_subnets

  eks_managed_node_groups = {
    default = {
      min_size       = 2
      max_size       = 4
      desired_size   = 2
      instance_types = ["t3.large"]
      iam_role_additional_policies = {
        ecr = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
        ebs = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
      }
    }
  }

  # EBS CSI driver for LGTM persistent volumes (Stage 2)
  cluster_addons = {
    aws-ebs-csi-driver = {}
    coredns            = {}
    kube-proxy         = {}
    vpc-cni            = {}
  }
}
```

`infra/terraform/modules/eks/variables.tf`:
```hcl
variable "cluster_name"    { type = string }
variable "vpc_id"          { type = string }
variable "private_subnets" { type = list(string) }
```

`infra/terraform/modules/eks/outputs.tf`:
```hcl
output "cluster_name"          { value = module.eks.cluster_name }
output "cluster_endpoint"      { value = module.eks.cluster_endpoint }
output "oidc_provider_arn"     { value = module.eks.oidc_provider_arn }
output "oidc_provider"         { value = module.eks.oidc_provider }
output "cluster_ca"            { value = module.eks.cluster_certificate_authority_data }
```

- [ ] **Step 2: Validate**

Run: `cd infra/terraform/modules/eks && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add infra/terraform/modules/eks/
git commit -m "feat(terraform): add EKS module with IRSA, ECR pull, EBS CSI"
```

---

### Task 5: ECR module

**Files:**
- Create: `infra/terraform/modules/ecr/main.tf`
- Create: `infra/terraform/modules/ecr/variables.tf`
- Create: `infra/terraform/modules/ecr/outputs.tf`

One repository per service, created from the `service_names` list — adding a service needs no module change, only a list entry.

- [ ] **Step 1: Write the module**

`infra/terraform/modules/ecr/main.tf`:
```hcl
resource "aws_ecr_repository" "service" {
  for_each             = toset(var.service_names)
  name                 = "foundry/${each.value}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "service" {
  for_each   = aws_ecr_repository.service
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 20 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}
```

`infra/terraform/modules/ecr/variables.tf`:
```hcl
variable "service_names" { type = list(string) }
```

`infra/terraform/modules/ecr/outputs.tf`:
```hcl
output "repository_urls" {
  value = { for k, r in aws_ecr_repository.service : k => r.repository_url }
}
```

- [ ] **Step 2: Validate**

Run: `cd infra/terraform/modules/ecr && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add infra/terraform/modules/ecr/
git commit -m "feat(terraform): add ECR module, one immutable repo per service"
```

---

### Task 6: IAM module — GitHub Actions OIDC + IRSA roles

**Files:**
- Create: `infra/terraform/modules/iam/github_oidc.tf`
- Create: `infra/terraform/modules/iam/irsa.tf`
- Create: `infra/terraform/modules/iam/variables.tf`
- Create: `infra/terraform/modules/iam/outputs.tf`

The GitHub OIDC role lets CI push to ECR and run Terraform with no stored keys. IRSA roles let in-cluster add-ons (ExternalDNS, ALB controller, External Secrets) and services assume scoped roles via their ServiceAccount.

- [ ] **Step 1: Write the GitHub OIDC provider + CI role**

`infra/terraform/modules/iam/github_oidc.tf`:
```hcl
data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:*"]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name               = "foundry-github-actions"
  assume_role_policy = data.aws_iam_policy_document.github_assume.json
}

# ECR push + Terraform state + describe permissions for CI
resource "aws_iam_role_policy_attachment" "github_ecr" {
  role       = aws_iam_role.github_actions.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser"
}
```

- [ ] **Step 2: Write the IRSA roles**

`infra/terraform/modules/iam/irsa.tf`:
```hcl
# Generic IRSA role factory: trusts a specific namespace/serviceaccount
locals {
  irsa_service_accounts = {
    external-dns                 = "kube-system:external-dns"
    aws-load-balancer-controller = "kube-system:aws-load-balancer-controller"
    external-secrets             = "external-secrets:external-secrets"
  }
}

data "aws_iam_policy_document" "irsa_assume" {
  for_each = local.irsa_service_accounts
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider}:sub"
      values   = ["system:serviceaccount:${each.value}"]
    }
  }
}

resource "aws_iam_role" "irsa" {
  for_each           = local.irsa_service_accounts
  name               = "foundry-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.irsa_assume[each.key].json
}

# ExternalDNS: manage Route 53 records
resource "aws_iam_role_policy" "external_dns" {
  name = "external-dns"
  role = aws_iam_role.irsa["external-dns"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["route53:ChangeResourceRecordSets"], Resource = ["arn:aws:route53:::hostedzone/${var.hosted_zone_id}"] },
      { Effect = "Allow", Action = ["route53:ListHostedZones", "route53:ListResourceRecordSets"], Resource = ["*"] }
    ]
  })
}

# External Secrets: read from Secrets Manager under foundry/*
resource "aws_iam_role_policy" "external_secrets" {
  name = "external-secrets"
  role = aws_iam_role.irsa["external-secrets"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      Resource = ["arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:foundry/*"]
    }]
  })
}

# AWS Load Balancer Controller: attach the official IAM policy (managed via a
# downloaded policy doc kept in the module for reproducibility)
resource "aws_iam_role_policy" "alb_controller" {
  name   = "alb-controller"
  role   = aws_iam_role.irsa["aws-load-balancer-controller"].id
  policy = file("${path.module}/alb-controller-policy.json")
}
```

`infra/terraform/modules/iam/variables.tf`:
```hcl
variable "github_repo"       { type = string }
variable "oidc_provider_arn" { type = string }
variable "oidc_provider"     { type = string }
variable "hosted_zone_id"    { type = string }
variable "region"            { type = string }
```

`infra/terraform/modules/iam/outputs.tf`:
```hcl
output "github_actions_role_arn" { value = aws_iam_role.github_actions.arn }
output "irsa_role_arns" {
  value = { for k, r in aws_iam_role.irsa : k => r.arn }
}
```

- [ ] **Step 3: Fetch the official ALB controller IAM policy**

Run:
```bash
curl -o infra/terraform/modules/iam/alb-controller-policy.json \
  https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v2.8.0/docs/install/iam_policy.json
```
Expected: a JSON file of ~250 lines is written.

- [ ] **Step 4: Validate**

Run: `cd infra/terraform/modules/iam && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/modules/iam/
git commit -m "feat(terraform): add IAM module — GitHub OIDC CI role + IRSA roles"
```

---

### Task 7: Wire modules in prod env + outputs

**Files:**
- Create: `infra/terraform/envs/prod/main.tf`
- Create: `infra/terraform/envs/prod/outputs.tf`

- [ ] **Step 1: Wire the modules**

`infra/terraform/envs/prod/main.tf`:
```hcl
module "vpc" {
  source       = "../../modules/vpc"
  cluster_name = var.cluster_name
}

module "eks" {
  source          = "../../modules/eks"
  cluster_name    = var.cluster_name
  vpc_id          = module.vpc.vpc_id
  private_subnets = module.vpc.private_subnets
}

module "ecr" {
  source        = "../../modules/ecr"
  service_names = var.service_names
}

module "iam" {
  source            = "../../modules/iam"
  github_repo       = var.github_repo
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider     = module.eks.oidc_provider
  hosted_zone_id    = var.hosted_zone_id
  region            = var.region
}
```

- [ ] **Step 2: Expose outputs**

`infra/terraform/envs/prod/outputs.tf`:
```hcl
output "cluster_name"            { value = module.eks.cluster_name }
output "ecr_repository_urls"     { value = module.ecr.repository_urls }
output "github_actions_role_arn" { value = module.iam.github_actions_role_arn }
output "irsa_role_arns"          { value = module.iam.irsa_role_arns }
```

- [ ] **Step 3: Init against the real backend and validate**

Run: `cd infra/terraform/envs/prod && terraform init && terraform validate`
Expected: backend initializes against S3; `Success! The configuration is valid.`

- [ ] **Step 4: Plan (read-only, safe)**

Run: `terraform plan`
Expected: a plan creating VPC, EKS, ECR, IAM resources. No errors. Do NOT apply yet — apply is gated through Task 8 or run manually by the operator.

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/envs/prod/main.tf infra/terraform/envs/prod/outputs.tf
git commit -m "feat(terraform): wire prod env (vpc, eks, ecr, iam) with outputs"
```

---

### Task 8: Terraform CI workflow (plan-on-PR, gated apply-on-merge)

**Files:**
- Create: `.github/workflows/terraform.yml`

- [ ] **Step 1: Write the workflow**

`.github/workflows/terraform.yml`:
```yaml
name: terraform

on:
  pull_request:
    paths: ["infra/terraform/**"]
  push:
    branches: [main]
    paths: ["infra/terraform/**"]

permissions:
  id-token: write   # required for OIDC
  contents: read
  pull-requests: write

env:
  TF_DIR: infra/terraform/envs/prod
  AWS_REGION: us-east-1

jobs:
  plan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::ACCOUNT_ID:role/foundry-github-actions
          aws-region: ${{ env.AWS_REGION }}
      - uses: hashicorp/setup-terraform@v3
      - name: Init
        working-directory: ${{ env.TF_DIR }}
        run: terraform init
      - name: Plan
        working-directory: ${{ env.TF_DIR }}
        run: terraform plan -no-color

  apply:
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    needs: plan
    runs-on: ubuntu-latest
    environment: production   # requires manual approval (configured in repo settings)
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::ACCOUNT_ID:role/foundry-github-actions
          aws-region: ${{ env.AWS_REGION }}
      - uses: hashicorp/setup-terraform@v3
      - name: Init
        working-directory: ${{ env.TF_DIR }}
        run: terraform init
      - name: Apply
        working-directory: ${{ env.TF_DIR }}
        run: terraform apply -auto-approve
```

- [ ] **Step 2: Replace ACCOUNT_ID**

After the first manual `terraform apply` (operator), get the account ID:
Run: `aws sts get-caller-identity --query Account --output text`
Then replace both `ACCOUNT_ID` occurrences in the workflow with that value.

- [ ] **Step 3: Configure the `production` environment protection rule**

In GitHub repo Settings → Environments → New environment `production` → add yourself as a required reviewer. This gates the `apply` job behind manual approval — the AWS analog of the `ready-for-merge` label gate.

- [ ] **Step 4: Validate workflow syntax**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/terraform.yml'))"`
Expected: no output (valid YAML).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/terraform.yml
git commit -m "feat(ci): add terraform plan-on-PR and gated apply-on-merge via OIDC"
```

**STAGE 1 GATE:** Operator runs the bootstrap (Task 1) then `terraform apply` in `envs/prod`. Verify: `aws eks update-kubeconfig --name foundry-prod --region us-east-1 && kubectl get nodes` shows 2 Ready nodes. Do not proceed to Stage 2 until this passes.

---

# Stage 2 — Platform Bootstrap

Install the cluster-level add-ons and the GitOps layer on EKS via ArgoCD. End state: ArgoCD reconciling `main`, ALB controller / ExternalDNS / cert-manager / External Secrets running, LGTM stack with persistent storage, Grafana reachable over a public ALB with TLS.

---

### Task 9: ArgoCD on EKS + extend app-of-apps to platform add-ons

**Files:**
- Modify: `infra/gitops/argo/app-of-apps.yaml`
- Create: `infra/gitops/argo/platform/` (directory)

ArgoCD itself is installed by the operator on the fresh cluster using the existing `infra/argo/helmfile.yaml` from Phase 3 (it is cluster-agnostic). This task points the app-of-apps at a new `platform/` directory so the add-ons reconcile via GitOps.

- [ ] **Step 1: Read the current app-of-apps to find its source path**

Run: `cat infra/gitops/argo/app-of-apps.yaml`
Expected: note the `source.path` (the directory ArgoCD scans for Application manifests).

- [ ] **Step 2: Confirm the app-of-apps scans `infra/gitops/argo/`**

If `source.path` is `infra/gitops/argo`, then any Application YAML placed under it — including in a `platform/` subdir — is picked up automatically (ArgoCD directory recursion). If recursion is off, add `directory: { recurse: true }` to the source:

`infra/gitops/argo/app-of-apps.yaml` (add under `source:` if missing):
```yaml
    directory:
      recurse: true
```

- [ ] **Step 3: Validate YAML**

Run: `python -c "import yaml; list(yaml.safe_load_all(open('infra/gitops/argo/app-of-apps.yaml')))"`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add infra/gitops/argo/app-of-apps.yaml
git commit -m "feat(gitops): enable recursive app-of-apps for platform add-ons"
```

---

### Task 10: AWS Load Balancer Controller

**Files:**
- Create: `infra/gitops/argo/platform/aws-load-balancer-controller.yaml`
- Create: `infra/gitops/argo/platform/values/aws-load-balancer-controller.yaml`

- [ ] **Step 1: Write the Argo Application**

`infra/gitops/argo/platform/aws-load-balancer-controller.yaml`:
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: aws-load-balancer-controller
  namespace: argocd
  finalizers: [resources-finalizer.argocd.io]
spec:
  project: default
  source:
    repoURL: https://aws.github.io/eks-charts
    chart: aws-load-balancer-controller
    targetRevision: 1.8.1
    helm:
      valueFiles: []
      values: |
        clusterName: foundry-prod
        serviceAccount:
          create: true
          name: aws-load-balancer-controller
          annotations:
            eks.amazonaws.com/role-arn: ARN_FROM_TF_OUTPUT  # irsa_role_arns["aws-load-balancer-controller"]
  destination:
    server: https://kubernetes.default.svc
    namespace: kube-system
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [CreateNamespace=true]
```

- [ ] **Step 2: Fill the role ARN**

Run: `cd infra/terraform/envs/prod && terraform output -json irsa_role_arns`
Replace `ARN_FROM_TF_OUTPUT` with the `aws-load-balancer-controller` value.

- [ ] **Step 3: Validate YAML**

Run: `python -c "import yaml; yaml.safe_load(open('infra/gitops/argo/platform/aws-load-balancer-controller.yaml'))"`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add infra/gitops/argo/platform/aws-load-balancer-controller.yaml
git commit -m "feat(gitops): add AWS Load Balancer Controller via ArgoCD"
```

---

### Task 11: ExternalDNS

**Files:**
- Create: `infra/gitops/argo/platform/external-dns.yaml`

- [ ] **Step 1: Write the Argo Application**

`infra/gitops/argo/platform/external-dns.yaml`:
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: external-dns
  namespace: argocd
  finalizers: [resources-finalizer.argocd.io]
spec:
  project: default
  source:
    repoURL: https://kubernetes-sigs.github.io/external-dns
    chart: external-dns
    targetRevision: 1.15.0
    helm:
      values: |
        provider: aws
        policy: sync
        domainFilters: ["foundry.example.com"]   # CHANGE to your domain
        serviceAccount:
          create: true
          name: external-dns
          annotations:
            eks.amazonaws.com/role-arn: ARN_FROM_TF_OUTPUT  # irsa_role_arns["external-dns"]
  destination:
    server: https://kubernetes.default.svc
    namespace: kube-system
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [CreateNamespace=true]
```

- [ ] **Step 2: Fill role ARN + domain**

Replace `ARN_FROM_TF_OUTPUT` (from `terraform output irsa_role_arns`) and the `domainFilters` value.

- [ ] **Step 3: Validate + commit**

Run: `python -c "import yaml; yaml.safe_load(open('infra/gitops/argo/platform/external-dns.yaml'))"`
```bash
git add infra/gitops/argo/platform/external-dns.yaml
git commit -m "feat(gitops): add ExternalDNS via ArgoCD"
```

---

### Task 12: cert-manager + External Secrets Operator

**Files:**
- Create: `infra/gitops/argo/platform/cert-manager.yaml`
- Create: `infra/gitops/argo/platform/external-secrets.yaml`

Note: with ALB + ACM, TLS certs are typically provisioned via ACM directly (referenced by ARN in the ingress annotation), so cert-manager is included for in-cluster cert needs (e.g. webhook certs, internal mTLS) rather than public TLS. ACM remains the public-cert path.

- [ ] **Step 1: Write cert-manager Application**

`infra/gitops/argo/platform/cert-manager.yaml`:
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: cert-manager
  namespace: argocd
  finalizers: [resources-finalizer.argocd.io]
spec:
  project: default
  source:
    repoURL: https://charts.jetstack.io
    chart: cert-manager
    targetRevision: v1.15.3
    helm:
      values: |
        crds:
          enabled: true
  destination:
    server: https://kubernetes.default.svc
    namespace: cert-manager
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [CreateNamespace=true]
```

- [ ] **Step 2: Write External Secrets Application**

`infra/gitops/argo/platform/external-secrets.yaml`:
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: external-secrets
  namespace: argocd
  finalizers: [resources-finalizer.argocd.io]
spec:
  project: default
  source:
    repoURL: https://charts.external-secrets.io
    chart: external-secrets
    targetRevision: 0.10.4
    helm:
      values: |
        installCRDs: true
        serviceAccount:
          create: true
          name: external-secrets
          annotations:
            eks.amazonaws.com/role-arn: ARN_FROM_TF_OUTPUT  # irsa_role_arns["external-secrets"]
  destination:
    server: https://kubernetes.default.svc
    namespace: external-secrets
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [CreateNamespace=true]
```

- [ ] **Step 3: Fill role ARN, validate, commit**

Replace `ARN_FROM_TF_OUTPUT` for external-secrets.
Run: `python -c "import yaml; yaml.safe_load(open('infra/gitops/argo/platform/cert-manager.yaml')); yaml.safe_load(open('infra/gitops/argo/platform/external-secrets.yaml'))"`
```bash
git add infra/gitops/argo/platform/cert-manager.yaml infra/gitops/argo/platform/external-secrets.yaml
git commit -m "feat(gitops): add cert-manager and External Secrets Operator via ArgoCD"
```

---

### Task 13: LGTM observability stack with EBS-backed storage

**Files:**
- Modify: `infra/grafana-stack/` Helmfile values (add `persistence` blocks)

The Phase 1–2 LGTM Helmfile runs on Kind with ephemeral storage. On EKS, Loki/Tempo/Prometheus need EBS-backed PersistentVolumes via the `gp3` StorageClass (the EBS CSI driver was enabled in Task 4).

- [ ] **Step 1: Inspect current Helmfile**

Run: `cat infra/grafana-stack/helmfile.yaml`
Expected: identify the releases (loki, tempo, prometheus, grafana, otel-collector) and their values files.

- [ ] **Step 2: Add persistence to each stateful release**

For Prometheus values, add:
```yaml
server:
  persistentVolume:
    enabled: true
    storageClass: gp3
    size: 20Gi
```
For Loki and Tempo, add the equivalent `persistence: { enabled: true, storageClassName: gp3, size: 10Gi }` block per their chart schema. Grafana:
```yaml
persistence:
  enabled: true
  storageClassName: gp3
  size: 5Gi
```

- [ ] **Step 3: Create the gp3 StorageClass manifest**

`infra/gitops/argo/platform/storageclass-gp3.yaml`:
```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
parameters:
  type: gp3
```

- [ ] **Step 4: Render-check the grafana-stack values**

Run: `helm template prometheus prometheus-community/prometheus -f <prometheus-values> | grep -A2 persistentVolume`
Expected: `enabled: true` and `storageClass: gp3` appear.

- [ ] **Step 5: Commit**

```bash
git add infra/grafana-stack/ infra/gitops/argo/platform/storageclass-gp3.yaml
git commit -m "feat(observability): EBS-backed persistence for LGTM stack on EKS"
```

**STAGE 2 GATE:** After operator syncs ArgoCD, verify all platform Applications are `Synced/Healthy` (`kubectl get applications -n argocd`), the ALB controller pod is Running, and a test Ingress provisions an ALB. Verify Grafana reachable at `https://grafana.foundry.<domain>` with valid TLS.

---

# Stage 3 — Service Migration

Migrate the chart, CI, and existing services to run on EKS. End state: `weather` and `player-projections` deployed via GitOps, reachable over public ALB URLs with TLS.

---

### Task 14: Add ServiceAccount template to generic-service chart

**Files:**
- Create: `helm/charts/generic-service/templates/serviceaccount.yaml`
- Modify: `helm/charts/generic-service/templates/deployment.yaml`
- Modify: `helm/charts/generic-service/values.yaml`

The chart currently has no ServiceAccount template — pods use `default`. IRSA requires a named ServiceAccount with a role-arn annotation.

- [ ] **Step 1: Write a failing render assertion**

Run: `helm template t helm/charts/generic-service -f helm/values/weather/values.yaml | grep "kind: ServiceAccount"`
Expected: no output (template does not exist yet) — this is the failing state.

- [ ] **Step 2: Add the values block**

In `helm/charts/generic-service/values.yaml`, add:
```yaml
serviceAccount:
  create: true
  name: ""          # defaults to service.name if empty
  annotations: {}   # e.g. eks.amazonaws.com/role-arn: arn:aws:iam::...:role/foundry-<svc>
```

- [ ] **Step 3: Write the ServiceAccount template**

`helm/charts/generic-service/templates/serviceaccount.yaml`:
```yaml
{{- if .Values.serviceAccount.create -}}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ .Values.serviceAccount.name | default .Values.service.name }}
  namespace: {{ .Release.Namespace }}
  {{- with .Values.serviceAccount.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
```

- [ ] **Step 4: Reference the ServiceAccount in the Deployment**

In `helm/charts/generic-service/templates/deployment.yaml`, inside the pod `spec:` (sibling of `containers:`), add:
```yaml
      serviceAccountName: {{ .Values.serviceAccount.name | default .Values.service.name }}
```

- [ ] **Step 5: Verify the render assertion passes**

Run: `helm template t helm/charts/generic-service -f helm/values/weather/values.yaml | grep "kind: ServiceAccount"`
Expected: `kind: ServiceAccount` now appears.

- [ ] **Step 6: Lint + commit**

Run: `helm lint helm/charts/generic-service -f helm/values/weather/values.yaml`
Expected: `1 chart(s) linted, 0 chart(s) failed`
```bash
git add helm/charts/generic-service/templates/serviceaccount.yaml helm/charts/generic-service/templates/deployment.yaml helm/charts/generic-service/values.yaml
git commit -m "feat(chart): add ServiceAccount template with IRSA annotation support"
```

---

### Task 15: Add Ingress template to generic-service chart

**Files:**
- Create: `helm/charts/generic-service/templates/ingress.yaml`
- Modify: `helm/charts/generic-service/values.yaml`

- [ ] **Step 1: Write a failing render assertion**

Run: `helm template t helm/charts/generic-service -f helm/values/weather/values.yaml --set ingress.enabled=true | grep "kind: Ingress"`
Expected: no output — failing state.

- [ ] **Step 2: Add the values block**

In `helm/charts/generic-service/values.yaml`, add:
```yaml
ingress:
  enabled: false
  className: alb
  annotations: {}   # ALB + ACM annotations set per-service
  host: ""          # e.g. weather.foundry.example.com
```

- [ ] **Step 3: Write the Ingress template**

`helm/charts/generic-service/templates/ingress.yaml`:
```yaml
{{- if .Values.ingress.enabled -}}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ .Values.service.name }}
  namespace: {{ .Release.Namespace }}
  {{- with .Values.ingress.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  ingressClassName: {{ .Values.ingress.className }}
  rules:
    - host: {{ .Values.ingress.host | quote }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ .Values.service.name }}
                port:
                  number: {{ .Values.service.port }}
{{- end }}
```

- [ ] **Step 4: Verify the assertion passes**

Run: `helm template t helm/charts/generic-service -f helm/values/weather/values.yaml --set ingress.enabled=true --set ingress.host=weather.foundry.example.com | grep "kind: Ingress"`
Expected: `kind: Ingress` appears.

- [ ] **Step 5: Lint + commit**

Run: `helm lint helm/charts/generic-service -f helm/values/weather/values.yaml`
Expected: `0 chart(s) failed`
```bash
git add helm/charts/generic-service/templates/ingress.yaml helm/charts/generic-service/values.yaml
git commit -m "feat(chart): add ALB Ingress template gated on ingress.enabled"
```

---

### Task 16: Migrate build-push action to ECR

**Files:**
- Modify: `.github/actions/build-push/action.yml`

- [ ] **Step 1: Rewrite the action for ECR**

Replace the body of `.github/actions/build-push/action.yml` with:
```yaml
name: Build and push image
description: Builds a Docker image from a service directory and pushes it to ECR

inputs:
  service:
    required: true
    description: Service name, used as the build context path (e.g. weather)
  image-name:
    required: true
    description: Full ECR image name (e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com/foundry/weather)
  tag:
    required: true
    description: Image tag (e.g. the Git SHA)

runs:
  using: composite
  steps:
    - uses: docker/setup-buildx-action@v3
    - uses: aws-actions/amazon-ecr-login@v2
    - uses: docker/build-push-action@v6
      with:
        context: services/${{ inputs.service }}
        push: true
        tags: ${{ inputs.image-name }}:${{ inputs.tag }}
        cache-from: type=gha
        cache-to: type=gha,mode=max
```

Note: `aws-actions/amazon-ecr-login@v2` requires AWS credentials already configured — the caller workflow (Task 17) adds `configure-aws-credentials` via OIDC before calling this action.

- [ ] **Step 2: Validate YAML**

Run: `python -c "import yaml; yaml.safe_load(open('.github/actions/build-push/action.yml'))"`
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add .github/actions/build-push/action.yml
git commit -m "feat(ci): migrate build-push from GHCR to ECR"
```

---

### Task 17: Update service workflows for OIDC + ECR

**Files:**
- Modify: `.github/workflows/weather.yml`
- Modify: `.github/workflows/player-projections.yml`

- [ ] **Step 1: Inspect the current weather workflow build-push job**

Run: `cat .github/workflows/weather.yml`
Expected: locate the `build-push` job and the `image-name` it passes (currently `ghcr.io/...`).

- [ ] **Step 2: Add OIDC permissions + AWS creds + ECR image name (weather)**

In the `build-push` job of `.github/workflows/weather.yml`:

Add at job level:
```yaml
    permissions:
      id-token: write
      contents: read
```
Add as the first step (before the build-push action):
```yaml
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::ACCOUNT_ID:role/foundry-github-actions
          aws-region: us-east-1
```
Change the `image-name` input passed to the build-push action to:
```yaml
          image-name: ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/foundry/weather
```

- [ ] **Step 3: Repeat for player-projections**

Apply the identical change to `.github/workflows/player-projections.yml`, using image path `foundry/player-projections`.

- [ ] **Step 4: Replace ACCOUNT_ID**

Replace every `ACCOUNT_ID` with the value from `aws sts get-caller-identity --query Account --output text`.

- [ ] **Step 5: Validate both workflows**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/weather.yml')); yaml.safe_load(open('.github/workflows/player-projections.yml'))"`
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/weather.yml .github/workflows/player-projections.yml
git commit -m "feat(ci): authenticate service builds via OIDC and push to ECR"
```

---

### Task 18: Prod GitOps overlays + prod Argo Applications

**Files:**
- Create: `infra/gitops/envs/prod/weather/values.yaml`
- Create: `infra/gitops/envs/prod/player-projections/values.yaml`
- Modify: `.github/actions/update-gitops-tag/action.yml`
- Modify: `infra/gitops/argo/weather.yaml`
- Modify: `infra/gitops/argo/player-projections.yaml`

The Phase 3 overlays live under `envs/local/`. Prod needs its own overlay with the ECR repository and prod image tags. The Argo Applications must also set `image.repository` to the ECR URL.

- [ ] **Step 1: Create the prod overlays**

`infra/gitops/envs/prod/weather/values.yaml`:
```yaml
image:
  repository: ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/foundry/weather
  tag: "0.1.0"
```
`infra/gitops/envs/prod/player-projections/values.yaml`:
```yaml
image:
  repository: ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/foundry/player-projections
  tag: "0.1.0"
```
Replace `ACCOUNT_ID` in both.

- [ ] **Step 2: Point the Argo Applications at the prod overlay**

In `infra/gitops/argo/weather.yaml`, change the second valueFile from the local overlay to:
```yaml
        - /infra/gitops/envs/prod/weather/values.yaml
```
Apply the equivalent change to `infra/gitops/argo/player-projections.yaml`.

(If you need both local and prod simultaneously, create separate `weather-prod.yaml` Applications instead — but for the AWS cutover, repoint the existing ones.)

- [ ] **Step 3: Update update-gitops-tag to write the prod path**

In `.github/actions/update-gitops-tag/action.yml`, change both `infra/gitops/envs/local/` occurrences to `infra/gitops/envs/prod/`:
```bash
        cat > infra/gitops/envs/prod/${{ inputs.service }}/values.yaml << 'EOF'
        image:
          repository: ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/foundry/${{ inputs.service }}
          tag: "${{ inputs.tag }}"
        EOF
```
And update the `git add` path to `infra/gitops/envs/prod/...`. Replace `ACCOUNT_ID`.

- [ ] **Step 4: Validate YAML**

Run: `python -c "import yaml; [yaml.safe_load(open(f)) for f in ['infra/gitops/envs/prod/weather/values.yaml','infra/gitops/envs/prod/player-projections/values.yaml','infra/gitops/argo/weather.yaml','infra/gitops/argo/player-projections.yaml']]"`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add infra/gitops/envs/prod/ infra/gitops/argo/weather.yaml infra/gitops/argo/player-projections.yaml .github/actions/update-gitops-tag/action.yml
git commit -m "feat(gitops): add prod overlays and point services at ECR images"
```

---

### Task 19: Enable ingress for weather and player-projections

**Files:**
- Modify: `helm/values/weather/values.yaml`
- Modify: `helm/values/player-projections/values.yaml`

- [ ] **Step 1: Add ingress block to weather**

In `helm/values/weather/values.yaml`, add:
```yaml
ingress:
  enabled: true
  className: alb
  host: weather.foundry.example.com   # CHANGE to your domain
  annotations:
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/certificate-arn: ACM_CERT_ARN  # from ACM
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'
```

- [ ] **Step 2: Add ingress block to player-projections**

Apply the same block to `helm/values/player-projections/values.yaml` with `host: player-projections.foundry.example.com`.

- [ ] **Step 3: Provision the ACM certificate (operator, one-time)**

Request a wildcard cert in ACM for `*.foundry.example.com`, validate via DNS (Route 53), and copy its ARN into both `certificate-arn` fields.

- [ ] **Step 4: Render-check**

Run: `helm template weather helm/charts/generic-service -f helm/values/weather/values.yaml | grep -A1 "kind: Ingress"`
Expected: an Ingress with the weather host renders.

- [ ] **Step 5: Lint + commit**

Run: `helm lint helm/charts/generic-service -f helm/values/weather/values.yaml && helm lint helm/charts/generic-service -f helm/values/player-projections/values.yaml`
Expected: `0 chart(s) failed`
```bash
git add helm/values/weather/values.yaml helm/values/player-projections/values.yaml
git commit -m "feat(services): expose weather and player-projections via ALB ingress"
```

---

### Task 20: Update CLAUDE.md + AWS bootstrap runbook

**Files:**
- Modify: `CLAUDE.md`
- Create: `docs/runbooks/aws-bootstrap.md`

- [ ] **Step 1: Add an AWS deployment section to CLAUDE.md**

Add a section documenting: Terraform lives in `infra/terraform/`; images go to ECR not GHCR; services get an IRSA role only if they need AWS access; ingress is enabled per-service via the `ingress` values block; the strict Terraform-owns-AWS / ArgoCD-owns-Kubernetes boundary. Reference `docs/architecture/phase-6-aws-deployment.md`.

- [ ] **Step 2: Write the bootstrap runbook**

`docs/runbooks/aws-bootstrap.md` — the ordered operator steps: (1) run `infra/terraform/bootstrap`, (2) set tfvars, (3) `terraform apply` in `envs/prod`, (4) `aws eks update-kubeconfig`, (5) install ArgoCD via `infra/argo/helmfile.yaml`, (6) fill role ARNs into platform Applications, (7) sync ArgoCD, (8) request ACM cert, (9) verify endpoints. Include the verification command for each step.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/runbooks/aws-bootstrap.md
git commit -m "docs(phase-6): document AWS deploy model and bootstrap runbook"
```

---

### Task 21: End-to-end validation (operator)

**Files:** none — verification only.

- [ ] **Step 1: Confirm services are live**

Run: `curl -sS https://weather.foundry.<domain>/health` and the player-projections health endpoint.
Expected: HTTP 200 with valid TLS (no cert warning).

- [ ] **Step 2: Confirm ArgoCD reconciliation**

Run: `kubectl get applications -n argocd`
Expected: `weather`, `player-projections`, and all `platform/*` apps show `Synced/Healthy`.

- [ ] **Step 3: Confirm a fresh CI build deploys**

Push a trivial change to `services/weather`. Verify CI pushes to ECR, `update-gitops-tag` writes `infra/gitops/envs/prod/weather/values.yaml`, and ArgoCD rolls the new tag.

- [ ] **Step 4: Validate the per-service delta doc**

Onboard a throwaway service using only Phase 2 + Phase 6 docs. Confirm the three additions (ingress block, optional IRSA annotation, optional ExternalSecret) are the entire AWS-specific delta. Tick the Stage 3 milestone in `docs/architecture/phase-6-aws-deployment.md`.

---

## Self-Review notes

- **Spec coverage:** Every Phase 6 spec section maps to tasks — Terraform layout (Tasks 1–7), ALB/Route53/ACM (Tasks 10, 15, 19), IRSA + OIDC (Tasks 6, 8, 17), ECR (Tasks 5, 16–18), LGTM on EKS (Task 13), ArgoCD app-of-apps (Task 9), per-service delta (Tasks 14–15, 19), ephemeral environments (deferred — module structure in Tasks 3–7 supports it, no task needed).
- **Chart gap found:** the `generic-service` chart had no ServiceAccount or Ingress template; Tasks 14–15 add them before services use them.
- **Placeholders:** `ACCOUNT_ID`, `ACM_CERT_ARN`, `ARN_FROM_TF_OUTPUT`, `foundry.example.com`, and `ZXXXXXXXXXXXXX` are account-specific values resolved from `terraform output` or AWS console at execution time — each has an explicit resolution step, not a "fill in later" gap.
- **Apply safety:** No task runs an unattended `terraform apply` against real AWS; applies are gated (Task 8 `production` environment) or operator-run at the Stage 1/2 gates.
```
