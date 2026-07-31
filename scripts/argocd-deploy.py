"""
Manage the Argo CD lifecycle: install, verify, promote, watch, and access the UI.

Usage:
  python scripts/argocd-deploy.py install  --env local [--context <ctx>]
  python scripts/argocd-deploy.py verify   --env local [--context <ctx>]
  python scripts/argocd-deploy.py promote  <service> --from <env> --to <env>
  python scripts/argocd-deploy.py watch    <service> --env local [--timeout 180]
  python scripts/argocd-deploy.py ui       [--port 8080] [--context <ctx>]
  python scripts/argocd-deploy.py help     [<command>]
"""

import argparse
import base64
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
GITOPS_ROOT = ROOT / "infra" / "gitops"
ARGO_DIR = ROOT / "infra" / "argo"
APPLICATIONSET = ROOT / "infra" / "gitops" / "argo" / "applicationset.yaml"

# The only environment the ApplicationSet generates Applications for. Not a
# preference — its template hard-codes `/infra/gitops/envs/local/` as the second
# valueFile and `namespace: default` on the in-cluster API server, so `local` is
# the whole set of envs that has a target. See `require_promotion_target`.
GENERATED_ENVS = ("local",)


# ── pure helpers ──────────────────────────────────────────────────────────────


def discover_services(env: str, gitops_root: Path = GITOPS_ROOT) -> list[str]:
    """List service names found under infra/gitops/envs/<env>/."""
    env_dir = gitops_root / "envs" / env
    if not env_dir.exists():
        return []
    return sorted(d.name for d in env_dir.iterdir() if d.is_dir())


def app_name(service: str, env: str) -> str:
    """Return the ArgoCD Application name for a service+env combo."""
    return service if env == "local" else f"{service}-{env}"


def write_tag(values_file: Path, tag: str) -> None:
    """Write image tag to a gitops values file, creating it if needed.

    Gitops env values files hold only the live image tag (`image.tag`); all
    other config lives in helm/values/<service>/values.yaml. If the file is
    missing or has no quoted tag line, it is (re)written to the canonical
    single-key form. This matches scripts/rollback.py and is safe because
    these files never carry other keys.
    """
    if values_file.exists():
        text = values_file.read_text()
        patched = re.sub(r'(tag:\s*")[^"]*(")', rf"\g<1>{tag}\2", text)
        if patched != text:
            values_file.write_text(patched)
            return
    values_file.parent.mkdir(parents=True, exist_ok=True)
    values_file.write_text(f'image:\n  tag: "{tag}"\n')


def read_tag(values_file: Path) -> str:
    """Read the current image tag from a gitops values file."""
    if not values_file.exists():
        print(f"Error: values file not found: {values_file}")
        sys.exit(1)
    text = values_file.read_text()
    m = re.search(r'tag:\s*"([^"]+)"', text)
    if not m:
        print(f"Error: no image.tag found in {values_file}")
        sys.exit(1)
    return m.group(1)


def argo_values_file(env: str, argo_dir: Path = ARGO_DIR) -> Path:
    """Return the helmfile values file for the given env (env-specific or default)."""
    env_specific = argo_dir / f"values-{env}.yaml"
    return env_specific if env_specific.exists() else argo_dir / "values.yaml"


# ── subprocess helpers ────────────────────────────────────────────────────────


def run(cmd: list, cwd: Path | None = None) -> None:
    """Run a subprocess, print the command, exit on non-zero."""
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    # check=False: the non-zero exit is handled below, and CalledProcessError's
    # traceback would bury the command output the caller actually needs.
    result = subprocess.run(cmd, cwd=cwd, check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


def _kubectl_cmd(args: tuple, context: str | None) -> list[str]:
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    return cmd + list(args)


def kubectl_run(*args: str, context: str | None = None) -> None:
    """Run kubectl, exit on non-zero."""
    run(_kubectl_cmd(args, context))


def kubectl_capture(*args: str, context: str | None = None) -> tuple[int, str]:
    """Run kubectl, return (returncode, stdout). Never exits."""
    cmd = _kubectl_cmd(args, context)
    # check=False: the returncode IS the return value — see the docstring.
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout.strip()


def helmfile_run(
    *args: str, context: str | None = None, cwd: Path | None = None
) -> None:
    """Run helmfile, passing --kube-context if provided."""
    cmd = ["helmfile"]
    if context:
        cmd += ["--kube-context", context]
    run(cmd + list(args), cwd=cwd)


def argo_password(context: str | None = None) -> str:
    """Decode the ArgoCD initial admin password from the cluster secret."""
    _, out = kubectl_capture(
        "get",
        "secret",
        "argocd-initial-admin-secret",
        "-n",
        "argocd",
        "-o",
        "jsonpath={.data.password}",
        context=context,
    )
    if not out:
        return "<not found>"
    return base64.b64decode(out).decode().strip()


# ── polling ───────────────────────────────────────────────────────────────────


def poll_applications(
    services: list[str],
    env: str,
    context: str | None,
    timeout: int,
    poll_interval: int = 3,
) -> bool:
    """Poll Application sync+health until all Synced+Healthy or timeout."""
    names = [app_name(s, env) for s in services]
    deadline = time.time() + timeout
    while time.time() < deadline:
        all_healthy = True
        for name in names:
            _, out = kubectl_capture(
                "get",
                "application",
                name,
                "-n",
                "argocd",
                "-o",
                "jsonpath={.status.sync.status},{.status.health.status}",
                context=context,
            )
            sync, _, health = out.partition(",")
            if sync != "Synced" or health != "Healthy":
                all_healthy = False
                print(f"  {name}: {sync or '?'}/{health or '?'}")
        if all_healthy:
            return True
        time.sleep(poll_interval)
    return False


# ── git + manifest helpers ────────────────────────────────────────────────────


def git_commit_and_push(files: list[Path], message: str) -> None:
    """Stage the given files, commit with message, and push."""
    for f in files:
        run(["git", "add", str(f)])
    run(["git", "commit", "-m", message])
    run(["git", "push"])


def require_promotion_target(service: str, env: str) -> None:
    """Exit unless `env` is an environment Argo CD actually generates apps for.

    **This used to write a manifest, and it must not any more.** It read
    `infra/gitops/argo/<service>.yaml` and copied it into
    `infra/gitops/argo/<service>-<env>.yaml` with the name and the env path
    rewritten. Phase 8 Wave 0 replaced every one of those per-service manifests
    with `infra/gitops/argo/applicationset.yaml`, so the source it read no
    longer exists — and its own error said "source manifest not found", which
    reads as somebody having forgotten a file rather than as the deliberate
    deletion it was.

    Restoring the copy is not the fix. Three things say so, and none of them is
    a matter of taste:

    * `tests/test_service_ci_coverage.py::test_argo_directory_holds_only_the_
      app_of_apps_and_the_set` asserts that directory holds exactly two files.
      A written `<service>-<env>.yaml` reds a green platform test.
    * The app-of-apps syncs that whole directory, so the written manifest WOULD
      be applied — into `namespace: default` on the same single cluster the
      local Application already owns.
    * Its HTTPRoute would carry `gateway.pathPrefix` out of
      `helm/values/<service>/values.yaml`, which is env-independent. Two envs
      would claim the identical path on the one `foundry` Gateway, and Gateway
      API breaks that tie on creation timestamp. That is a manifest which
      applies cleanly and then misroutes silently — strictly worse than a loud
      refusal.

    So this refuses, and says why. Making promotion real is an ApplicationSet
    change plus an env strategy that does not exist yet: cluster-vs-namespace,
    per-env gateway paths, per-env Secrets. `infra/gitops/envs/staging/` and
    `envs/prod/` hold a `.gitkeep` and nothing else, so there is no target to
    promote into today either way.
    """
    if env in GENERATED_ENVS:
        return

    print(
        f"Error: cannot promote {service!r} to {env!r} — no {env} environment "
        f"exists to promote into.\n"
        f"\n"
        f"  {APPLICATIONSET.relative_to(ROOT).as_posix()} generates every Argo CD\n"
        f"  Application from helm/values/*, and its template hard-codes the local\n"
        f"  env: /infra/gitops/envs/local/<service>/values.yaml, namespace\n"
        f"  'default'. There is no {service}-{env} Application for a promoted tag\n"
        f"  to reach.\n"
        f"\n"
        f"  The per-service manifests this command used to copy\n"
        f"  (infra/gitops/argo/{service}.yaml) were deleted deliberately in Phase 8\n"
        f"  Wave 0. They are not missing files, and re-adding one is a regression:\n"
        f"  tests/test_service_ci_coverage.py fails on any third file in that\n"
        f"  directory, and the app-of-apps would apply it into the same namespace\n"
        f"  and onto the same gateway path the local Application already holds.\n"
        f"\n"
        f"  Nothing was written and nothing was committed.\n"
        f"\n"
        f"  Promotion needs the ApplicationSet to become env-aware first, which\n"
        f"  needs an env strategy nobody has designed: whether a non-local env is\n"
        f"  a separate cluster or a namespace, how each env gets its own gateway\n"
        f"  path, and where its Secrets come from. That is Phase 6's work — see\n"
        f"  docs/architecture/phase-6-aws-deployment.md."
    )
    sys.exit(1)


# ── sub-commands ──────────────────────────────────────────────────────────────


def cmd_install(args) -> None:
    ctx = args.context
    env = args.env

    print(f"\nInstalling Argo CD for env '{env}'...")
    values = argo_values_file(env)
    helmfile_run("repos", context=ctx, cwd=ARGO_DIR)
    helmfile_run("apply", "--values", str(values), context=ctx, cwd=ARGO_DIR)

    print("\nWaiting for argocd-server to be ready...")
    kubectl_run(
        "wait",
        "--for=condition=available",
        "deployment/argocd-server",
        "-n",
        "argocd",
        "--timeout=180s",
        context=ctx,
    )

    print("\nApplying app-of-apps...")
    kubectl_run(
        "apply",
        "-f",
        str(ROOT / "infra/gitops/argo/app-of-apps.yaml"),
        context=ctx,
    )

    print("\nWaiting for all Applications to be Synced + Healthy...")
    services = discover_services(env)
    wait_only = getattr(args, "wait_only", None)
    if wait_only:
        # Scope the wait to what the caller actually needs ready.
        #
        # The ApplicationSet globs helm/values/*, so Argo manages an
        # Application for EVERY service -- but a collector's GitOps overlay
        # pins tag 0.1.0 until build-push publishes its first image on merge
        # to main. On a PR branch those images do not exist, so those
        # Applications can never reach Healthy and an unscoped wait always
        # times out. A caller that exercises only weather should not block
        # on collectors whose images have never been published.
        missing = sorted(set(wait_only) - set(services))
        if missing:
            print(f"  Requested services not discovered: {', '.join(missing)}")
            sys.exit(1)
        services = [s for s in services if s in set(wait_only)]
        print(f"  Waiting only on: {', '.join(services)}")
    if services:
        ok = poll_applications(services, env, ctx, timeout=300)
        if not ok:
            print("Timeout: not all Applications reached Synced+Healthy within 300s")
            sys.exit(1)
    else:
        print(f"  No services found in infra/gitops/envs/{env}/ — skipping sync wait.")

    pwd = argo_password(ctx)
    print(f"\n{'=' * 50}")
    print(f"Argo CD installed. Admin password: {pwd}")
    print(
        "Run 'python scripts/argocd-deploy.py ui' to access the UI at http://localhost:8080"
    )
    print("=" * 50)


def cmd_verify(args) -> None:
    ctx = args.context
    env = args.env

    print(f"\nVerifying Argo CD ({env})...")

    rc, out = kubectl_capture(
        "get", "pods", "-n", "argocd", "--no-headers", context=ctx
    )
    if rc != 0:
        print("Error: could not list argocd pods — is the cluster reachable?")
        sys.exit(1)

    pod_lines = [ln for ln in out.splitlines() if ln.strip()]
    not_running = [ln for ln in pod_lines if "Running" not in ln]
    if not_running:
        print("Some Argo CD pods are not Running:")
        for ln in not_running:
            print(f"  {ln}")
        sys.exit(1)
    print(f"  Pods: {len(pod_lines)} Running")

    services = discover_services(env)
    if not services:
        print(f"  No services in infra/gitops/envs/{env}/ — nothing to check.")
        return

    failed = []
    print(f"\n  {'Application':<30} {'Sync':<12} {'Health':<12} Last Sync")
    print(f"  {'-' * 70}")
    for svc in services:
        name = app_name(svc, env)
        kubectl_capture(
            "annotate",
            "application",
            name,
            "-n",
            "argocd",
            "argocd.argoproj.io/refresh=normal",
            "--overwrite",
            context=ctx,
        )
        _, status_out = kubectl_capture(
            "get",
            "application",
            name,
            "-n",
            "argocd",
            "-o",
            "jsonpath={.status.sync.status},{.status.health.status},{.status.operationState.finishedAt}",
            context=ctx,
        )
        parts = (status_out + ",,").split(",")
        sync, health, last_sync = parts[0], parts[1], parts[2]
        print(f"  {name:<30} {sync:<12} {health:<12} {last_sync}")
        if sync != "Synced" or health != "Healthy":
            failed.append(name)

    if failed:
        print(f"\nNot Synced+Healthy: {', '.join(failed)}")
        sys.exit(1)
    print("\nAll Applications: Synced + Healthy")


def cmd_promote(args) -> None:
    service = args.service
    from_env = args.from_env
    to_env = args.to_env
    ctx = args.context

    if from_env == to_env:
        print(f"Error: --from and --to must differ (both are '{from_env}')")
        sys.exit(1)

    from_file = GITOPS_ROOT / "envs" / from_env / service / "values.yaml"
    to_file = GITOPS_ROOT / "envs" / to_env / service / "values.yaml"

    tag = read_tag(from_file)
    print(f"\nPromoting {service}: {from_env} -> {to_env} @ {tag}")

    # Checked BEFORE write_tag, so a refused promotion leaves the tree clean.
    # A promotion commits only the image tag now — the Application it lands on
    # is generated, never written here. See `require_promotion_target`.
    require_promotion_target(service, to_env)
    write_tag(to_file, tag)

    git_commit_and_push(
        [to_file],
        f"chore(gitops): promote {service} from {from_env} to {to_env} @ {tag}",
    )

    print(f"\nWaiting for {app_name(service, to_env)} to sync in {to_env}...")
    ok = poll_applications([service], to_env, ctx, timeout=args.timeout)
    if not ok:
        print(
            f"Timeout: {app_name(service, to_env)} did not reach "
            f"Synced+Healthy within {args.timeout}s"
        )
        sys.exit(1)
    print(f"\nDone. {service} @ {tag} is live in {to_env}.")


def cmd_watch(args) -> None:
    service = args.service
    env = args.env
    ctx = args.context

    print(f"\nWatching {service} rollout in '{env}'...")

    print("\n--- kubectl rollout status ---")
    rollout_cmd = _kubectl_cmd(
        (
            "rollout",
            "status",
            f"deployment/{service}",
            "-n",
            "default",
            f"--timeout={args.timeout}s",
        ),
        ctx,
    )
    # check=False deliberately: a non-zero exit here is informational only —
    # Argo CD's Application health is the authoritative gate below, and a
    # transient rollout timeout may still reconcile to Healthy.
    rollout = subprocess.run(rollout_cmd, check=False)
    if rollout.returncode != 0:
        print(
            f"  (kubectl rollout status exited {rollout.returncode}; "
            "checking Argo CD state)"
        )

    print("\n--- Application status ---")
    name = app_name(service, env)
    ok = poll_applications([service], env, ctx, timeout=args.timeout)
    if not ok:
        print(f"{name} did not reach Synced+Healthy within {args.timeout}s.")
        sys.exit(1)
    print(f"{name}: Synced + Healthy")


def cmd_ui(args) -> None:
    ctx = args.context
    port = args.port

    pf_cmd = _kubectl_cmd(
        ("port-forward", "svc/argocd-server", "-n", "argocd", f"{port}:80"),
        ctx,
    )
    proc = subprocess.Popen(
        pf_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1)

    pwd = argo_password(ctx)
    print(f"\n{'=' * 50}")
    print(f"Argo CD UI:  http://localhost:{port}")
    print("Username:    admin")
    print(f"Password:    {pwd}")
    print("=" * 50)
    print("Press Ctrl+C to stop the port-forward.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping port-forward...")
        proc.terminate()
        print("Done.")


# ── CLI ───────────────────────────────────────────────────────────────────────


def cmd_help(args, parser: argparse.ArgumentParser) -> None:
    if args.topic:
        for action in parser._subparsers._actions:
            if (
                hasattr(action, "_name_parser_map")
                and args.topic in action._name_parser_map
            ):
                action._name_parser_map[args.topic].print_help()
                return
        print(f"Unknown command: {args.topic}")
    parser.print_help()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="argocd-deploy",
        description=(
            "Manage the Argo CD lifecycle: install, verify, promote, watch, "
            "and access the UI."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # install
    p = sub.add_parser("install", help="Install Argo CD and bootstrap app-of-apps")
    p.add_argument(
        "--wait-only",
        nargs="+",
        metavar="SERVICE",
        help=(
            "Wait for only these Applications. The ApplicationSet generates "
            "one per helm/values/* dir, and a collector's overlay pins 0.1.0 "
            "until its image is first published on merge to main -- so on a "
            "PR branch an unscoped wait cannot pass."
        ),
    )
    p.add_argument(
        "--env",
        default="local",
        choices=["local", "staging", "prod"],
        help="Target environment (default: local)",
    )
    p.add_argument(
        "--context", default=None, help="kubectl context (default: active context)"
    )
    p.set_defaults(func=cmd_install)

    # verify
    p = sub.add_parser(
        "verify", help="Read-only health check: pods, sync status, repo reachability"
    )
    p.add_argument(
        "--env",
        default="local",
        choices=["local", "staging", "prod"],
        help="Target environment (default: local)",
    )
    p.add_argument(
        "--context", default=None, help="kubectl context (default: active context)"
    )
    p.set_defaults(func=cmd_verify)

    # promote
    p = sub.add_parser(
        "promote",
        help="Promote a service image tag from one env to another",
        description=(
            "Copy a service's image tag from one env's gitops values file to "
            "another's, commit, push, and wait for the target Application to "
            "sync. Only 'local' has generated Applications today — "
            "infra/gitops/argo/applicationset.yaml hard-codes that env — so "
            "--to staging|prod refuses with an explanation rather than "
            "committing a tag nothing will read."
        ),
    )
    p.add_argument("service", help="Service name (e.g. weather)")
    p.add_argument(
        "--from",
        dest="from_env",
        required=True,
        choices=["local", "staging", "prod"],
        help="Source environment",
    )
    p.add_argument(
        "--to",
        dest="to_env",
        required=True,
        choices=["local", "staging", "prod"],
        help="Target environment",
    )
    p.add_argument(
        "--context",
        default=None,
        help="kubectl context for watching target env sync (default: active context)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for sync (default: 300)",
    )
    p.set_defaults(func=cmd_promote)

    # watch
    p = sub.add_parser(
        "watch", help="Stream rollout status and confirm Application is Synced+Healthy"
    )
    p.add_argument("service", help="Service name (e.g. weather)")
    p.add_argument(
        "--env",
        default="local",
        choices=["local", "staging", "prod"],
        help="Target environment (default: local)",
    )
    p.add_argument(
        "--context", default=None, help="kubectl context (default: active context)"
    )
    p.add_argument(
        "--timeout", type=int, default=180, help="Seconds to wait (default: 180)"
    )
    p.set_defaults(func=cmd_watch)

    # ui
    p = sub.add_parser("ui", help="Port-forward the Argo CD UI and print credentials")
    p.add_argument(
        "--context", default=None, help="kubectl context (default: active context)"
    )
    p.add_argument("--port", type=int, default=8080, help="Local port (default: 8080)")
    p.set_defaults(func=cmd_ui)

    # help
    p = sub.add_parser("help", help="Show help for a command")
    p.add_argument("topic", nargs="?", default=None, help="Command to get help for")
    p.set_defaults(func=lambda a: cmd_help(a, parser))

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
