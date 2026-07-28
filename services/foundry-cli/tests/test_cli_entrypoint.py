import json

import pytest

from foundry import cli


class FakeBundle:
    def to_dict(self):
        return {"service": "weather", "suspects": []}


def test_no_args_exits_two_with_usage(capsys):
    """A required subparser means bare invocation is an argparse error."""
    with pytest.raises(SystemExit) as exc:
        cli.main([])

    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_help_flag_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])

    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_unknown_command_exits_two():
    with pytest.raises(SystemExit) as exc:
        cli.main(["definitely-not-a-command"])

    assert exc.value.code == 2


def test_triage_requires_service_flag():
    with pytest.raises(SystemExit) as exc:
        cli.main(["triage"])

    assert exc.value.code == 2


def test_parser_defaults():
    args = cli.build_parser().parse_args(["triage", "--service", "weather"])

    assert args.command == "triage"
    assert args.service == "weather"
    assert args.endpoint is None
    assert args.incident == ""
    assert args.prometheus_url == "http://localhost:9090"
    assert args.gitops_dir == "infra/gitops"
    assert args.json is False


def test_triage_json_flag_prints_bundle_and_skips_narrator(monkeypatch, capsys):
    """--json emits only the EvidenceBundle; the LLM narrator is never called."""
    narrated = []
    monkeypatch.setattr("foundry.triage.pipeline.detect", lambda **kwargs: FakeBundle())
    monkeypatch.setattr(
        "foundry.triage.narrator.narrate", lambda b: narrated.append(b) or "x"
    )

    code = cli.main(["triage", "--service", "weather", "--json"])

    assert code == 0
    assert narrated == []
    assert json.loads(capsys.readouterr().out) == {
        "service": "weather",
        "suspects": [],
    }


def test_triage_without_json_prints_narrative(monkeypatch, capsys):
    monkeypatch.setattr("foundry.triage.pipeline.detect", lambda **kwargs: FakeBundle())
    monkeypatch.setattr(
        "foundry.triage.narrator.narrate", lambda b: "the deploy did it"
    )

    code = cli.main(["triage", "--service", "weather"])
    out = capsys.readouterr().out

    assert code == 0
    assert "=== Triage narrative ===" in out
    assert "the deploy did it" in out


def test_detect_receives_parsed_arguments(monkeypatch, capsys):
    """Flags must reach the pipeline unchanged."""
    seen = {}

    def fake_detect(**kwargs):
        seen.update(kwargs)
        return FakeBundle()

    monkeypatch.setattr("foundry.triage.pipeline.detect", fake_detect)

    cli.main(
        [
            "triage",
            "--service",
            "player-projections",
            "--endpoint",
            "/projections",
            "--incident",
            "error rate spike",
            "--prometheus-url",
            "http://prom:9090",
            "--gitops-dir",
            "custom/gitops",
            "--json",
        ]
    )

    assert seen == {
        "service": "player-projections",
        "endpoint": "/projections",
        "description": "error rate spike",
        "prometheus_url": "http://prom:9090",
        "gitops_dir": "custom/gitops",
    }
