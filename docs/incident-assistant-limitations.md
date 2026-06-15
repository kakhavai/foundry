# Incident Detection and Triage Engine — Limitations

This document describes what the Phase 4 triage tool does not do and why. These are deliberate design boundaries, not gaps to fill in a later patch.

---

## No autonomous remediation

The triage engine is assistive only. It never triggers a rollback, adjusts replica counts, suppresses alerts, modifies configuration, or takes any action against the cluster.

Autonomous remediation requires a level of system understanding and failure-mode coverage that is hard to verify. A bad auto-rollback during a database migration can cause more damage than the original incident. The right boundary for AI in incident response is: surface context faster, reduce cognitive load, suggest next checks. The engineer retains full agency over what happens next.

---

## The narrator never re-detects or invents signals

`narrator.py` (Phase 4B) receives a structured `EvidenceBundle` and explains it. It does not re-query Prometheus, Loki, Tempo, or the GitOps log. It does not re-rank suspects. It does not invent anomalies that are absent from the bundle.

If the detection engine (Phase 4A) did not flag a signal, the narrator will not mention it — even if the narrative prompt might lead a human to wonder about it. Detection is 4A's job. Keeping those responsibilities separate is what makes the system evaluable: detection accuracy can be measured independently of the LLM.

---

## Milestone 1 covers metric and deploy signals only

The Milestone 1 detection spine covers two signal types:

- **Metric anomalies** — error rate, p95 latency, request volume via Prometheus.
- **Deploy correlation** — recent deploys from the GitOps git log, scored by proximity and path overlap.

Log-template novelty (Drain-style templating against Loki) and trace-based fault localization (downstream span suspect ranking from Tempo) are **Milestone 2** work, deferred to a follow-on implementation plan. The `SuspectScore` dataclass reserves `log_support` and `trace_support` slots so Milestone 2 lights them up without a refactor, but they are 0 in all Milestone 1 output.

---

## Baselines are point-in-time Prometheus queries, not a continuously-maintained model

The detection engine computes a baseline by querying a trailing window (default 24 hours) from Prometheus at the moment `foundry triage` is invoked. There is no background job, no persisted baseline store, and no model that learns over time.

This means:

- The baseline reflects what the service did over the last 24 hours, not over a longer history.
- If the service was already degraded during the baseline window, the detector will underestimate how abnormal current behavior is.
- Sudden traffic seasonality (e.g. game-day spikes) can appear anomalous against a non-game-day baseline.

These are acceptable tradeoffs for an on-demand CLI. A continuously-maintained baseline store is a clean future upgrade but is not needed until Phase 4 moves to continuous detection.

---

## The fault toggle is for the eval harness only

`FAULT_UPSTREAM_LATENCY_MS`, `FAULT_UPSTREAM_ERROR_RATE`, and `FAULT_BREAK_ROUTE` are env-var-guarded fault behaviors in the `weather` upstream client, present solely to give the Phase 4 eval harness reproducible incidents to measure accuracy against.

The fault toggle is not a chaos engineering framework. It covers only the `weather` service's upstream calls, exposes only three failure modes, and is not suitable for testing infrastructure-level failures.

Phase 5 ships a proper chaos layer (Chaos Mesh) that supersedes the fault toggle for all resilience and adversarial testing purposes. Once Phase 5 is in place, the fault toggle's only remaining use is bootstrapping the Phase 4 eval in environments where the Phase 5 chaos stack is not running.
