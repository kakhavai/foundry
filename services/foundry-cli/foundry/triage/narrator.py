import json
import os

from foundry.triage.models.evidence import EvidenceBundle

_MODEL = "claude-opus-4-8"

_SYSTEM = (
    "You are an incident triage assistant. You are given a structured evidence bundle "
    "produced by a deterministic detection engine: metric anomalies, deploy events, "
    "and ranked suspects. Your job is to NARRATE this evidence for an on-call engineer "
    "— you do not re-run detection, invent signals, or re-rank the suspects. Explain, "
    "in this order: (1) what is abnormal, (2) what changed recently, (3) which "
    "suspects are best supported and why, (4) what checks would reduce uncertainty, "
    "(5) what NOT to assume. Be concise and calibrated about uncertainty. Never "
    "recommend an automated remediation."
)


def build_prompt(bundle: EvidenceBundle) -> tuple[str, str]:
    """Return (system, user) prompt strings.

    The user message is the evidence bundle JSON."""
    user = (
        "Here is the incident evidence bundle. Narrate it for the on-call engineer.\n\n"
        + json.dumps(bundle.to_dict(), indent=2)
    )
    return _SYSTEM, user


def narrate(bundle: EvidenceBundle, client=None, api_key: str | None = None) -> str:
    """Send the evidence bundle to Claude and return the triage narrative. Pass `client`
    to inject a fake in tests. With no client and no API key, returns a clear message
    rather than raising — detection (`--json`) does not depend on the LLM."""

    system, user = build_prompt(bundle)

    if client is None:
        key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            return (
                "[narrative skipped: ANTHROPIC_API_KEY is not set. The evidence "
                "bundle above is the detection output; set the key to get an LLM "
                "narrative, or use --json for detection only.]"
            )
        import anthropic

        client = anthropic.Anthropic(api_key=key)

    response = client.messages.create(
        model=_MODEL,
        max_tokens=2000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in response.content if b.type == "text")
