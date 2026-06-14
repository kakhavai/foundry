import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="foundry", description="Foundry platform CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    triage = sub.add_parser("triage", help="Run incident detection and triage")
    triage.add_argument("--service", required=True, help="Service name, e.g. weather")
    triage.add_argument(
        "--endpoint", default=None, help="Affected route, e.g. /activity"
    )
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
        import json

        from foundry.triage.pipeline import detect

        bundle = detect(
            service=args.service,
            endpoint=args.endpoint,
            description=args.incident,
            prometheus_url=args.prometheus_url,
            gitops_dir=args.gitops_dir,
        )

        if args.json:
            print(json.dumps(bundle.to_dict(), indent=2))
            return 0

        from foundry.triage.narrator import narrate

        print(json.dumps(bundle.to_dict(), indent=2))
        print("\n=== Triage narrative ===\n")
        print(narrate(bundle))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
