from types import SimpleNamespace

from foundry.triage.models.evidence import (
    DeployEvent,
    EvidenceBundle,
    MetricAnomaly,
    Suspect,
)
from foundry.triage.models.incident import Incident
from foundry.triage.narrator import build_prompt, narrate


def _bundle():
    return EvidenceBundle(
        incident=Incident("weather", "/activity", "elevated error rate"),
        metric_anomalies=[MetricAnomaly("error_rate", 0.124, 0.002, 8.7)],
        deploy_events=[DeployEvent("abc1234", 3.0, ["services/weather/x.py"], 0.73)],
        suspects=[
            Suspect("weather deploy abc1234", 0.73, ["onset 3 min after deploy"])
        ],
    )


def test_build_prompt_includes_evidence_json():
    system, user = build_prompt(_bundle())
    assert "weather" in user
    assert "error_rate" in user
    assert "abc1234" in user
    # The system prompt must constrain the model to narration, not re-detection.
    assert "narrat" in system.lower() or "explain" in system.lower()


def test_narrate_uses_injected_client_and_returns_text():
    class FakeMessages:
        def create(self, **kwargs):
            assert kwargs["model"] == "claude-opus-4-8"
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="The error rate is abnormal...")
                ]
            )

    fake_client = SimpleNamespace(messages=FakeMessages())
    narrative = narrate(_bundle(), client=fake_client)
    assert "abnormal" in narrative


def test_narrate_handles_missing_api_key_gracefully():
    # With no client and no key, narrate returns a clear message rather than raising.
    narrative = narrate(_bundle(), client=None, api_key="")
    assert "ANTHROPIC_API_KEY" in narrative
