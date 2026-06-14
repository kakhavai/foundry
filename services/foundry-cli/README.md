# foundry-cli

Platform CLI for Foundry. Subcommands:

- `foundry triage --service <name> [--endpoint /route] [--incident "..."]` — runs the
  Incident Detection Engine (4A) to produce a structured evidence bundle, then the LLM
  Triage Assistant (4B) to narrate it. `--json` emits only the bundle (no LLM call).

Detection is deterministic and independently testable; the LLM only explains the evidence.
