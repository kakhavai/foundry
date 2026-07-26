from foundry.triage.models.incident import Incident


def test_incident_to_dict():
    incident = Incident(service="weather", endpoint="/activity", description="errors")
    assert incident.to_dict() == {
        "service": "weather",
        "endpoint": "/activity",
        "description": "errors",
    }


def test_incident_endpoint_optional():
    incident = Incident(service="weather", endpoint=None, description="errors")
    assert incident.to_dict()["endpoint"] is None
