"""Tests for scripts/refresh-collector.py.

Lives in the repo-root platform suite for the same reason the rollback and
load-harness tests do: an operator CLI that drives the whole collector fleet is
a platform concern no per-service suite can see.

**No cluster and no network.** Every transport is either a pure function
(`refresh_url`, `pod_command`, `parse_pod_output`) or monkeypatched at the
module boundary, exactly as `tests/test_run_load.py` mocks `run-load.py`'s
cluster calls.

The token used throughout is `SECRET_TOKEN`, a value that appears nowhere else
in the repo, so `assert SECRET_TOKEN not in output` is a real assertion rather
than a coincidence.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Import refresh-collector.py (hyphenated) as a module — same pattern as
# tests/test_run_load.py and tests/test_argocd_deploy.py.
spec = importlib.util.spec_from_file_location(
    "refresh_collector", ROOT / "scripts" / "refresh-collector.py"
)
rc = importlib.util.module_from_spec(spec)
sys.modules["refresh_collector"] = rc
spec.loader.exec_module(rc)

SECRET_TOKEN = "swordfish-not-in-any-other-file"


# ── synthetic fleet ───────────────────────────────────────────────────────────
#
# Built from the real `collectors.Service`, so a change to that dataclass breaks
# these loudly rather than letting them pass against a stale hand-rolled stub.
# Synthetic rather than the live registry on purpose: the behaviours under test
# (ordering, dedup, unknown-name rejection) must not change their meaning the
# day a twenty-fifth collector is appended. One test below does run against the
# real registry, to prove the wiring.


def _collector(name: str, port: int, *, capture_enabled: bool = True):
    return rc.fleet.Service(
        name=name,
        port=port,
        is_collector=True,
        path=f"/collectors/{name}",
        token_secret=f"{name}-collector-token",
        lake_secret=f"{name}-lake-credentials",
        capture_enabled=capture_enabled,
    )


ALPHA = _collector("alpha", 8000)
BRAVO = _collector("bravo", 8001)
QUIET = _collector("charlie", 8002, capture_enabled=False)
NOT_A_COLLECTOR = rc.fleet.Service(name="ledger", port=8009, is_collector=False)
FLEET = [ALPHA, BRAVO, QUIET, NOT_A_COLLECTOR]


# ── target resolution ─────────────────────────────────────────────────────────


def test_resolves_one_named_collector():
    assert rc.resolve_targets(["bravo"], all_collectors=False, services=FLEET) == [
        BRAVO
    ]


def test_resolves_several_in_the_order_named():
    targets = rc.resolve_targets(
        ["bravo", "alpha"], all_collectors=False, services=FLEET
    )
    assert [t.name for t in targets] == ["bravo", "alpha"]


def test_all_selects_every_collector_and_nothing_else():
    """The count is asserted explicitly. `set(...) == {...}` would still pass if
    resolution silently dropped a duplicate it should have kept, and a bare
    membership check would pass on a list of one."""
    targets = rc.resolve_targets([], all_collectors=True, services=FLEET)
    assert [t.name for t in targets] == ["alpha", "bravo", "charlie"]
    assert len(targets) == 3


def test_all_excludes_non_collector_services():
    """`player-projections` has no /refresh. Including it would produce a 404
    on every fleet run and teach operators to ignore the summary."""
    targets = rc.resolve_targets([], all_collectors=True, services=FLEET)
    assert NOT_A_COLLECTOR not in targets


def test_unknown_collector_is_rejected_by_name():
    with pytest.raises(rc.UsageError) as exc:
        rc.resolve_targets(["wether"], all_collectors=False, services=FLEET)
    assert "wether" in str(exc.value)


def test_unknown_collector_error_lists_the_valid_names():
    """Failing loudly is only half of it — the message has to say what *would*
    have worked, or the operator's next move is to go read the registry."""
    with pytest.raises(rc.UsageError) as exc:
        rc.resolve_targets(["wether"], all_collectors=False, services=FLEET)
    message = str(exc.value)
    assert "alpha" in message
    assert "bravo" in message
    assert "charlie" in message


def test_a_non_collector_service_name_is_rejected_like_a_typo():
    with pytest.raises(rc.UsageError) as exc:
        rc.resolve_targets(["ledger"], all_collectors=False, services=FLEET)
    assert "ledger" in str(exc.value)


def test_one_unknown_name_among_valid_ones_rejects_the_whole_run():
    """Partial dispatch would be worse than no dispatch: the operator sees a
    summary of two successes and has to notice the third name is missing."""
    with pytest.raises(rc.UsageError):
        rc.resolve_targets(["alpha", "nope"], all_collectors=False, services=FLEET)


def test_repeated_names_are_dispatched_once():
    """Two requests inside the interval floor means the second is a 429 — a
    self-inflicted failure that would make the run exit non-zero."""
    targets = rc.resolve_targets(
        ["alpha", "alpha"], all_collectors=False, services=FLEET
    )
    assert targets == [ALPHA]
    assert len(targets) == 1


def test_all_with_explicit_names_is_a_usage_error():
    with pytest.raises(rc.UsageError):
        rc.resolve_targets(["alpha"], all_collectors=True, services=FLEET)


def test_naming_nothing_without_all_is_a_usage_error():
    """Not a silent no-op: a bare invocation must say what to do next."""
    with pytest.raises(rc.UsageError):
        rc.resolve_targets([], all_collectors=False, services=FLEET)


def test_a_fleet_with_no_collectors_is_a_usage_error():
    """--all over an empty registry would otherwise succeed having refreshed
    nothing — the same vacuous pass check-registry.py guards against."""
    with pytest.raises(rc.UsageError):
        rc.resolve_targets([], all_collectors=True, services=[NOT_A_COLLECTOR])


def test_targets_come_from_the_real_registry():
    """The wiring test: names are resolved through scripts/collectors.py, so a
    collector appended to contracts/collector-registry.yaml is refreshable with
    no edit here. Asserts against `weather`, the one collector whose removal
    would be a phase-level event rather than routine churn."""
    services = rc.fleet.services()
    targets = rc.resolve_targets(["weather"], all_collectors=False, services=services)
    assert [t.name for t in targets] == ["weather"]
    assert targets[0].path == "/collectors/weather"


def test_every_registered_collector_is_reachable_by_all():
    services = rc.fleet.services()
    registered = [s.name for s in services if s.is_collector]
    targets = rc.resolve_targets([], all_collectors=True, services=services)
    assert [t.name for t in targets] == registered
    assert len(targets) > 0


# ── scope ─────────────────────────────────────────────────────────────────────


def test_no_scope_flags_sends_an_empty_body():
    """Empty means "each collector's own CAPTURE_SEASON/CAPTURE_WEEK". A
    season guessed from the wall clock here would diverge from the deployment's
    configured week the moment an operator advanced one."""
    assert rc.build_scope(None, None) == {}


def test_season_alone_is_sent_alone():
    """`/refresh` reads season and week independently, so a half-specified
    scope is meaningful — the other half falls back to the collector."""
    assert rc.build_scope(2026, None) == {"season": 2026}


def test_week_alone_is_sent_alone():
    assert rc.build_scope(None, 4) == {"week": 4}


def test_both_are_sent_together():
    assert rc.build_scope(2026, 4) == {"season": 2026, "week": 4}


# ── interpreting the reply ────────────────────────────────────────────────────


def test_202_is_accepted_and_names_the_refresh_id():
    body = json.dumps({"refresh_id": "deadbeef", "scope": {"season": 2026, "week": 4}})
    outcome = rc.interpret_response("alpha", 202, body)
    assert outcome.status == rc.ACCEPTED
    assert outcome.accepted is True
    assert "deadbeef" in outcome.detail


def test_202_echoes_the_scope_the_collector_actually_used():
    """The collector fills in whatever the caller omitted, so its echoed scope
    is the only place the operator learns which week was captured."""
    body = json.dumps({"refresh_id": "abc", "scope": {"season": 2026, "week": 7}})
    outcome = rc.interpret_response("alpha", 202, body)
    assert '"week": 7' in outcome.detail


def test_202_with_an_unreadable_body_is_still_accepted():
    """The status code is the contract; the body is a courtesy. A 202 whose
    body got truncated still means the capture was dispatched, and reporting it
    as a failure would send an operator chasing a refresh that is running."""
    outcome = rc.interpret_response("alpha", 202, "not json at all")
    assert outcome.accepted is True
    assert "none returned" in outcome.detail


def test_202_with_valid_json_that_is_not_an_object_is_still_accepted():
    """`json.loads("null")` returns None, and `.get` on it raises — on the
    success path, where a crash is least excusable."""
    outcome = rc.interpret_response("alpha", 202, "null")
    assert outcome.accepted is True


def test_429_is_rate_limited_not_a_crash():
    outcome = rc.interpret_response("alpha", 429, '{"detail": "too soon"}', "180")
    assert outcome.status == rc.RATE_LIMITED
    assert outcome.accepted is False


def test_429_reports_the_retry_after_and_names_the_setting():
    """An operator who hits the floor needs two things: how long to wait, and
    which knob decides that."""
    outcome = rc.interpret_response("alpha", 429, "{}", "180")
    assert "180s" in outcome.detail
    assert "REFRESH_MIN_INTERVAL_SECONDS" in outcome.detail


def test_429_without_a_retry_after_header_still_reads_cleanly():
    outcome = rc.interpret_response("alpha", 429, "{}", None)
    assert outcome.status == rc.RATE_LIMITED
    assert "None" not in outcome.detail


def test_401_says_the_token_was_rejected():
    outcome = rc.interpret_response("alpha", 401, "{}")
    assert outcome.status == rc.FAILED
    assert "token was rejected" in outcome.detail


def test_403_is_treated_as_a_rejected_token():
    assert rc.interpret_response("alpha", 403, "{}").status == rc.FAILED


def test_503_points_at_the_unsynced_secret():
    """The documented trap: a Healthy pod that 503s every data route because
    its Secret never arrived. Saying "HTTP 503" alone sends the reader to the
    deploy, which is fine."""
    outcome = rc.interpret_response("alpha", 503, "{}")
    assert outcome.status == rc.FAILED
    assert "Secret" in outcome.detail


def test_404_points_at_the_gateway_public_paths():
    outcome = rc.interpret_response("alpha", 404, "")
    assert outcome.status == rc.FAILED
    assert "publicPaths" in outcome.detail


def test_an_unrecognized_status_still_fails_with_its_code():
    outcome = rc.interpret_response("alpha", 500, "")
    assert outcome.status == rc.FAILED
    assert "500" in outcome.detail


def test_no_status_other_than_202_is_ever_accepted():
    """The polarity guard. `accepted` drives the exit code, so a mutation that
    widened the 202 check (>= 200, `in range(200, 300)`) has to fail here."""
    for status in (200, 201, 204, 301, 400, 401, 403, 404, 429, 500, 502, 503):
        assert rc.interpret_response("alpha", status, "{}").accepted is False, status


def test_a_response_body_is_never_echoed_into_the_detail():
    """Nothing a collector returns carries a token today. Keeping bodies out of
    the report keeps that true by construction rather than by review of every
    future error path."""
    body = json.dumps({"detail": f"token {SECRET_TOKEN} rejected"})
    outcome = rc.interpret_response("alpha", 401, body)
    assert SECRET_TOKEN not in outcome.detail


# ── the summary and the exit code ─────────────────────────────────────────────


def _outcome(name: str, status: str) -> "rc.Outcome":
    return rc.Outcome(name, status, "detail")


def test_all_accepted_exits_zero():
    code, _ = rc.summarize([_outcome("a", rc.ACCEPTED), _outcome("b", rc.ACCEPTED)])
    assert code == 0


def test_one_failure_among_successes_exits_nonzero():
    code, lines = rc.summarize(
        [
            _outcome("a", rc.ACCEPTED),
            _outcome("b", rc.FAILED),
            _outcome("c", rc.ACCEPTED),
        ]
    )
    assert code == 1
    assert any("2/3 accepted" in line for line in lines)


def test_a_partial_failure_is_visible_in_the_report_too():
    """Both halves of the requirement: the exit code and the output. A CI job
    reads the code; a human reads the lines."""
    _, lines = rc.summarize([_outcome("a", rc.ACCEPTED), _outcome("b", rc.FAILED)])
    text = "\n".join(lines)
    assert "a" in text and "b" in text
    assert rc.FAILED in text


def test_a_rate_limited_refresh_is_not_an_accepted_refresh():
    """429 means the capture was NOT dispatched. Treating it as success because
    it is an "expected" outcome would report a refresh that never happened."""
    code, _ = rc.summarize([_outcome("a", rc.ACCEPTED), _outcome("b", rc.RATE_LIMITED)])
    assert code == 1


def test_every_outcome_rate_limited_still_exits_nonzero():
    code, _ = rc.summarize([_outcome("a", rc.RATE_LIMITED)])
    assert code == 1


def test_an_empty_run_does_not_pass():
    """`all([])` is True. A loop that dispatched nothing — every target skipped,
    a transport that never ran — would otherwise exit 0 reporting success for
    zero refreshes. This is the exact vacuous-truth defect 8A shipped at 98.5%
    coverage."""
    code, lines = rc.summarize([])
    assert code == 1
    assert any("nothing was refreshed" in line for line in lines)


def test_the_summary_counts_rather_than_folding_booleans():
    """A large mixed run must still land on the arithmetic, not on whichever
    outcome happened to be last."""
    outcomes = [_outcome(f"c{i}", rc.ACCEPTED) for i in range(9)]
    outcomes.insert(4, _outcome("bad", rc.FAILED))
    code, lines = rc.summarize(outcomes)
    assert code == 1
    assert any("9/10 accepted" in line for line in lines)


# ── URL and pod-exec construction ─────────────────────────────────────────────


def test_refresh_url_uses_the_registry_path():
    assert (
        rc.refresh_url("http://localhost:8080", ALPHA)
        == "http://localhost:8080/collectors/alpha/refresh"
    )


def test_refresh_url_tolerates_a_trailing_slash():
    assert "//collectors" not in rc.refresh_url("http://localhost:8080/", ALPHA)


def test_refresh_url_never_carries_the_token():
    """The token belongs in a header. A URL reaches logs, proxies, and the
    console line this tool prints before every request."""
    assert SECRET_TOKEN not in rc.refresh_url("http://localhost:8080", ALPHA)


def test_pod_command_execs_the_collectors_own_deployment():
    command = rc.pod_command(ALPHA, {"week": 4}, 15.0, "default")
    assert command[:2] == ["kubectl", "exec"]
    assert "deploy/alpha" in command
    assert "-n" in command and "default" in command


def test_pod_command_targets_the_port_from_the_helm_values():
    command = rc.pod_command(BRAVO, {}, 15.0, "default")
    assert "8001" in command


def test_pod_command_honours_a_custom_namespace():
    command = rc.pod_command(ALPHA, {}, 15.0, "staging")
    assert "staging" in command


def test_pod_command_carries_no_token(monkeypatch):
    """The whole point of --via-pod: the token stays inside the pod, so it
    never enters an argv that `ps` and the audit log can both read."""
    monkeypatch.setenv("COLLECTOR_TOKEN", SECRET_TOKEN)
    command = rc.pod_command(ALPHA, {"season": 2026}, 15.0, "default")
    assert SECRET_TOKEN not in " ".join(command)


def test_pod_command_is_an_argv_not_a_shell_string():
    """No shell in the chain means nothing to quote wrong — on Windows least
    of all."""
    command = rc.pod_command(ALPHA, {}, 15.0, "default")
    assert isinstance(command, list)
    assert all(isinstance(part, str) for part in command)
    assert len(command) > 5


def test_pod_script_reads_the_token_from_the_pods_environment():
    assert 'os.environ.get("COLLECTOR_TOKEN"' in rc.POD_SCRIPT


# ── reading the in-pod reply ──────────────────────────────────────────────────


def test_parses_the_marked_reply_line():
    line = rc.POD_MARKER + json.dumps(
        {"status": 202, "body": '{"refresh_id": "x"}', "retry_after": None}
    )
    status, body, retry_after = rc.parse_pod_output(line + "\n")
    assert status == 202
    assert retry_after is None


def test_ignores_kubectl_chatter_before_the_marker():
    """`kubectl exec` prints "Defaulted container ..." on a multi-container pod,
    and it lands on the same stream."""
    noise = "Defaulted container 'weather' out of: weather, istio-proxy\n"
    line = rc.POD_MARKER + json.dumps({"status": 202, "body": "{}"})
    status, _, _ = rc.parse_pod_output(noise + line + "\n")
    assert status == 202


def test_an_in_pod_error_is_a_transport_error_not_a_crash():
    line = rc.POD_MARKER + json.dumps({"error": "URLError: connection refused"})
    with pytest.raises(rc.TransportError) as exc:
        rc.parse_pod_output(line)
    assert "connection refused" in str(exc.value)


def test_no_marker_at_all_is_a_transport_error():
    with pytest.raises(rc.TransportError):
        rc.parse_pod_output("python3: command not found\n")


def test_a_malformed_marked_line_is_a_transport_error():
    with pytest.raises(rc.TransportError):
        rc.parse_pod_output(rc.POD_MARKER + "{not json")


# ── refresh_one never raises ──────────────────────────────────────────────────


def _fixed_transport(monkeypatch, *, result=None, raises=None):
    def fake(*_args, **_kwargs):
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(rc, "post_refresh_http", fake)
    monkeypatch.setattr(rc, "post_refresh_pod", fake)


def _refresh(service=ALPHA, **overrides):
    kwargs = {
        "token": SECRET_TOKEN,
        "scope": {},
        "timeout": 1.0,
        "base_url": "http://localhost:8080",
        "via_pod": False,
        "namespace": "default",
    }
    kwargs.update(overrides)
    return rc.refresh_one(service, **kwargs)


def test_an_unreachable_collector_becomes_a_failed_outcome(monkeypatch):
    _fixed_transport(monkeypatch, raises=rc.TransportError("unreachable"))
    outcome = _refresh()
    assert outcome.status == rc.FAILED
    assert outcome.name == "alpha"


def test_an_unexpected_exception_becomes_a_failed_outcome(monkeypatch):
    """One collector's surprise must not abort a fleet run and lose the summary
    for every collector after it — the same reasoning run-chaos.py applies to
    --all."""
    _fixed_transport(monkeypatch, raises=RuntimeError("something odd"))
    outcome = _refresh()
    assert outcome.status == rc.FAILED
    assert "RuntimeError" in outcome.detail


def test_via_pod_uses_the_pod_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rc,
        "post_refresh_pod",
        lambda *a, **k: calls.append("pod") or (202, '{"refresh_id": "p"}', None),
    )
    monkeypatch.setattr(
        rc,
        "post_refresh_http",
        lambda *a, **k: calls.append("http") or (202, "{}", None),
    )
    outcome = _refresh(via_pod=True)
    assert calls == ["pod"]
    assert outcome.accepted is True


# ── main: exit codes and the token guarantee ──────────────────────────────────


def _stub_fleet(monkeypatch, services=None):
    monkeypatch.setattr(rc.fleet, "services", lambda: list(services or FLEET))


def _record_transport(monkeypatch, replies):
    """Serve `replies` (status, body, retry_after) in order, recording targets."""
    seen = []

    def fake_http(url, token, scope, timeout):
        seen.append(url)
        return replies[len(seen) - 1]

    monkeypatch.setattr(rc, "post_refresh_http", fake_http)
    return seen


def test_main_exits_zero_when_every_refresh_is_accepted(monkeypatch, capsys):
    _stub_fleet(monkeypatch)
    _record_transport(
        monkeypatch,
        [(202, '{"refresh_id": "a"}', None), (202, '{"refresh_id": "b"}', None)],
    )
    code = rc.main(["alpha", "bravo", "--token", SECRET_TOKEN])
    assert code == 0
    assert "2/2 accepted" in capsys.readouterr().out


def test_main_exits_nonzero_on_a_partial_failure(monkeypatch, capsys):
    _stub_fleet(monkeypatch)
    _record_transport(
        monkeypatch, [(202, '{"refresh_id": "a"}', None), (500, "", None)]
    )
    code = rc.main(["alpha", "bravo", "--token", SECRET_TOKEN])
    assert code == 1
    out = capsys.readouterr().out
    assert "1/2 accepted" in out
    assert "bravo" in out


def test_main_contacts_every_target_even_after_one_fails(monkeypatch):
    """A partial failure has to be partial: the second collector still gets its
    request. An early `return` on the first failure would make the summary a
    lie about what was attempted."""
    _stub_fleet(monkeypatch)
    seen = _record_transport(monkeypatch, [(500, "", None), (202, "{}", None)])
    rc.main(["alpha", "bravo", "--token", SECRET_TOKEN])
    assert len(seen) == 2


def test_main_exits_nonzero_on_a_rate_limited_refresh(monkeypatch, capsys):
    _stub_fleet(monkeypatch)
    _record_transport(monkeypatch, [(429, '{"detail": "too soon"}', "240")])
    code = rc.main(["alpha", "--token", SECRET_TOKEN])
    assert code == 1
    out = capsys.readouterr().out
    assert rc.RATE_LIMITED in out
    assert "240s" in out
    assert "Traceback" not in out


def test_main_rejects_an_unknown_collector_without_contacting_anything(
    monkeypatch, capsys
):
    _stub_fleet(monkeypatch)
    seen = _record_transport(monkeypatch, [])
    code = rc.main(["wether", "--token", SECRET_TOKEN])
    assert code == 2
    assert seen == []
    assert "unknown collector" in capsys.readouterr().err


def test_main_requires_a_token_for_the_http_transport(monkeypatch, capsys):
    _stub_fleet(monkeypatch)
    monkeypatch.delenv("COLLECTOR_TOKEN", raising=False)
    code = rc.main(["alpha"])
    assert code == 2
    assert "no bearer token" in capsys.readouterr().err


def test_main_accepts_the_token_from_the_environment(monkeypatch):
    _stub_fleet(monkeypatch)
    monkeypatch.setenv("COLLECTOR_TOKEN", SECRET_TOKEN)
    sent = {}

    def fake_http(url, token, scope, timeout):
        sent["token"] = token
        return 202, '{"refresh_id": "x"}', None

    monkeypatch.setattr(rc, "post_refresh_http", fake_http)
    assert rc.main(["alpha"]) == 0
    assert sent["token"] == SECRET_TOKEN


def test_main_refuses_token_plus_via_pod(monkeypatch, capsys):
    """--via-pod uses the pod's own token, so a --token here would never be
    sent. Ignoring it silently would leave an operator believing they had
    authenticated with a token that went nowhere.

    The pod transport is stubbed even though this run must never reach it —
    without that, deleting the guard makes this test shell out to a real
    `kubectl exec`, which is exactly the cluster dependency this suite forbids.
    """
    _stub_fleet(monkeypatch)
    monkeypatch.setattr(
        rc, "post_refresh_pod", lambda *a, **k: pytest.fail("dispatched anyway")
    )
    code = rc.main(["alpha", "--via-pod", "--token", SECRET_TOKEN])
    assert code == 2
    assert "--via-pod" in capsys.readouterr().err


def test_main_needs_no_token_for_via_pod(monkeypatch):
    _stub_fleet(monkeypatch)
    monkeypatch.delenv("COLLECTOR_TOKEN", raising=False)
    monkeypatch.setattr(
        rc, "post_refresh_pod", lambda *a, **k: (202, '{"refresh_id": "x"}', None)
    )
    assert rc.main(["alpha", "--via-pod"]) == 0


def test_main_passes_the_scope_through_to_the_request(monkeypatch):
    _stub_fleet(monkeypatch)
    sent = {}

    def fake_http(url, token, scope, timeout):
        sent.update(scope)
        return 202, "{}", None

    monkeypatch.setattr(rc, "post_refresh_http", fake_http)
    rc.main(["alpha", "--season", "2026", "--week", "4", "--token", SECRET_TOKEN])
    assert sent == {"season": 2026, "week": 4}


def test_main_warns_when_a_targets_capture_loop_is_disabled(monkeypatch, capsys):
    """CAPTURE_ENABLED=false turns off the cadence loop, not the upstream call.
    A dispatched refresh still reaches the third party, which is why CLAUDE.md
    forbids it from smoke hooks — an operator deserves the same warning."""
    _stub_fleet(monkeypatch)
    _record_transport(monkeypatch, [(202, "{}", None)])
    rc.main(["charlie", "--token", SECRET_TOKEN])
    assert "CAPTURE_ENABLED is false" in capsys.readouterr().out


def test_main_says_accepted_is_not_done(monkeypatch, capsys):
    """The one thing this tool must not imply. 202 is a dispatch receipt, and
    the output has to say so or an operator will read the summary as proof the
    capture landed."""
    _stub_fleet(monkeypatch)
    _record_transport(monkeypatch, [(202, '{"refresh_id": "a"}', None)])
    rc.main(["alpha", "--token", SECRET_TOKEN])
    out = capsys.readouterr().out
    assert "Accepted is not done" in out
    assert "last_capture_at" in out


# ── the token never reaches the output ────────────────────────────────────────
#
# One test per path a token could plausibly escape through: the happy path, an
# authentication failure (where the token is the subject of the message), a
# transport exception (whose text is built from the URL), and a usage error.


@pytest.mark.parametrize(
    "reply",
    [
        (202, '{"refresh_id": "a"}', None),
        (401, '{"detail": "Invalid or missing bearer token"}', None),
        (429, '{"detail": "refresh requested too soon"}', "300"),
        (503, '{"detail": "Collector token is not configured"}', None),
    ],
)
def test_the_token_never_appears_in_output(monkeypatch, capsys, reply):
    _stub_fleet(monkeypatch)
    _record_transport(monkeypatch, [reply])
    rc.main(["alpha", "--token", SECRET_TOKEN])
    captured = capsys.readouterr()
    assert SECRET_TOKEN not in captured.out
    assert SECRET_TOKEN not in captured.err


def test_the_token_never_appears_when_the_transport_raises(monkeypatch, capsys):
    """The error text is built from the URL, which is the most likely place a
    credential would ride along if one were ever put there."""
    _fixed_transport(
        monkeypatch,
        raises=rc.TransportError(
            "http://localhost:8080/collectors/alpha/refresh is unreachable"
        ),
    )
    _stub_fleet(monkeypatch)
    rc.main(["alpha", "--token", SECRET_TOKEN])
    captured = capsys.readouterr()
    assert SECRET_TOKEN not in captured.out
    assert SECRET_TOKEN not in captured.err


def test_the_token_never_appears_on_a_usage_error(monkeypatch, capsys):
    _stub_fleet(monkeypatch)
    rc.main(["wether", "--token", SECRET_TOKEN])
    captured = capsys.readouterr()
    assert SECRET_TOKEN not in captured.out
    assert SECRET_TOKEN not in captured.err


def test_the_token_never_appears_in_the_printed_request_line(monkeypatch, capsys):
    """The tool echoes the URL it is about to POST to. That line is the one an
    operator copies into a bug report."""
    _stub_fleet(monkeypatch)
    _record_transport(monkeypatch, [(202, "{}", None)])
    rc.main(["alpha", "--token", SECRET_TOKEN])
    out = capsys.readouterr().out
    assert "POST http://localhost:8080/collectors/alpha/refresh" in out
    assert SECRET_TOKEN not in out
