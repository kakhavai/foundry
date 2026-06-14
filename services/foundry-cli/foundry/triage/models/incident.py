from dataclasses import dataclass


@dataclass
class Incident:
    """The incident under investigation: a service, an optional route, and a
    description."""

    service: str
    endpoint: str | None
    description: str

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "endpoint": self.endpoint,
            "description": self.description,
        }
