import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="foundry", description="Foundry platform CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    triage = sub.add_parser("triage", help="Run incident detection and triage")
    triage.add_argument("--service", required=True, help="Service name, e.g. weather")
    triage.add_argument("--endpoint", default=None, help="Affected route, e.g. /activity")
    triage.add_argument("--incident", default="", help="Free-text incident description")
    triage.add_argument(
        "--prometheus-url",
        default="http://localhost:9090",
        help="Prometheus base URL",
    )
    triage.add_argument(
        "--gitops-dir",
        default="infra/gitops",
        help="Path to the GitOps directory (for deploy history)",
    )
    triage.add_argument(
        "--json",
        action="store_true",
        help="Emit only the EvidenceBundle JSON (skip the LLM narrative)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "triage":
        # Wired to the detection pipeline in Task 10.
        print(f"triage: service={args.service}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
