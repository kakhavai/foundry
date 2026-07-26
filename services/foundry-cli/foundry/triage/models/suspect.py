from dataclasses import dataclass, field


@dataclass
class SuspectScore:
    """A ranking-model row. Each signal contributes a support term; contradictions
    subtract. log_support and trace_support stay 0 until the Milestone 2 detectors
    exist — the slots are reserved now so the ranker needs no refactor later."""

    suspect: str
    metric_support: float = 0.0
    log_support: float = 0.0
    trace_support: float = 0.0
    deploy_support: float = 0.0
    contradiction_penalty: float = 0.0
    evidence: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return (
            self.metric_support
            + self.log_support
            + self.trace_support
            + self.deploy_support
            - self.contradiction_penalty
        )
